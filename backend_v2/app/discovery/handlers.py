from __future__ import annotations

import time
import uuid
from pathlib import Path

from app.core.db import SessionLocal
from app.core.errors import BadRequest, ServiceUnavailable
from app.core.settings import get_settings
from app.core.time import utcnow
from app.discovery import sessions
from app.discovery.sweep import PortalSweeper, SweepResult, organisation_ages
from app.discovery.intel import CorpusIntelligenceService
from app.discovery.browser import (
    SESSION_LOST_NOTE,
    BrowserDriverUnavailable,
    SessionLost,
    build_driver,
)
from app.discovery.captcha import parse_captcha_form
from app.discovery.documents import DocumentFetchService, enqueue_fetch
from app.discovery.portal_walk import PortalWalker
from app.discovery.relevance import understanding_terms_from_rows
from app.discovery.service import DiscoveryService, union_profile
from app.discovery.tender_extraction import TERMINATION_MESSAGE
from app.models.identity import Company
from app.repositories.corpus import (
    DocumentRepository,
    TenderAuxRepository,
    TenderRepository,
)
from app.repositories.understanding import CompanyUnderstandingRepository
from app.services.job_events import (
    register_bidding_fetch,
    register_job_closed,
)
from app.services.job_registry import register_handler
from app.services.job_types import JobParked, JobResult
from app.services.jobs import JobService

#: A resumed job whose browser session is gone starts again from the start.
#: That is the only honest fallback: the portal binds a challenge and a file to
#: the session that opened them, so the old page cannot be reattached.
RESTARTED_NOTE = (
    "The browser session from the previous attempt was gone (the process "
    "restarted or the window was closed), so this attempt started again from "
    "the beginning."
)


def handle_scrape_portal(context) -> JobResult:
    """Walk the portal in one browser session, then store what was read.

    The whole walk runs on the thread that owns the browser (``sessions``), so
    a resume after ``needs_you`` reaches the same page even when another worker
    claims the job. A real blocking challenge parks the job and keeps that
    thread alive, so the human answers in the visible window and the walk
    resumes in the same session. The walk restarts from the directory after the
    handoff: the portal's cookie survives, and re-reading is idempotent. A
    second challenge is reported rather than parked again, and a headless
    browser cannot hand off at all because there is no window to answer in.
    """

    settings = get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable("Tender discovery is not enabled in this deployment.")

    with SessionLocal() as session:
        companies = list(
            session.query(Company).filter(Company.is_active.is_(True)).all()
        )
        understanding = CompanyUnderstandingRepository(session)
        profile = union_profile(
            companies,
            understanding_terms={
                company.id: understanding_terms_from_rows(
                    understanding.confirmed_capabilities(company.id),
                    understanding.confirmed_experiences(company.id),
                )
                for company in companies
            },
        )
    if not (
        profile.get("understanding")
        or profile.get("products_services")
        or profile.get("preferences", {}).get("keywords")
    ):
        return JobResult(
            state="partial",
            notes=(
                "No active company profile declares a sector, service or "
                "keyword, or has a confirmed capability."
            ),
            metrics={"stored": 0, "updated": 0, "rejected": 0},
        )

    run = sessions.get(context.job_id)
    fresh = run is None
    # A job that had parked before and has no session left was restarted by the
    # lease, not by a human: the old browser is gone and this is a new attempt.
    restarted = fresh and _previously_parked(context)
    if fresh:
        lane = sessions.open_lane(context.job_id, lambda: build_driver(settings))
        try:
            run = lane.call(
                lambda driver: sessions.WalkSession(
                    job_id=context.job_id,
                    driver=driver,
                    created_at=time.monotonic(),
                )
            )
        except BrowserDriverUnavailable as exc:
            sessions.close(context.job_id)
            raise ServiceUnavailable(str(exc)) from exc
        except Exception:
            sessions.close(context.job_id)
            raise
        sessions.put(run, lane)
    else:
        lane = run.lane or sessions.put(run)
        # A parked job only leaves ``needs_you`` after the human signalled
        # Continue, so a session that is still here has been answered once.
        run.answered_challenges += 1

    try:
        result = lane.call(lambda driver: _walk_body(context, settings, run, profile))
    except SessionLost:
        sessions.close(context.job_id)
        return JobResult(state="partial", notes=SESSION_LOST_NOTE)
    if lane.lost:
        sessions.close(context.job_id)
        return JobResult(
            state="partial", notes=SESSION_LOST_NOTE, metrics=result.metrics
        )
    return _with_note(result, RESTARTED_NOTE) if restarted else result


def _walk_body(context, settings, run, profile) -> JobResult:
    """The walk itself, running on the thread that owns the browser."""

    driver = run.driver
    headless = bool(getattr(driver, "headless", False))
    # The walk can take minutes; keep the window small and covered until the
    # portal actually needs the human.
    driver.begin_work()

    try:
        walker = PortalWalker(
            driver,
            profile,
            directory_url=settings.discovery_directory_url,
            max_pages=settings.agent_walk_max_pages,
            max_notices=min(
                settings.agent_walk_max_notices, settings.discovery_max_tenders
            ),
            max_notices_per_org=settings.agent_walk_max_notices_per_org,
            max_org_pages=settings.agent_walk_max_org_pages,
            today=utcnow().date(),
            on_step=context.heartbeat,
        )
        walked = walker.walk()
    except Exception:
        sessions.close(context.job_id)
        raise

    if walked.challenge is not None:
        if not headless and run.answered_challenges == 0:
            # The window stays minimized: the picture is shown in the app and
            # the answer is typed there, so there is nothing to uncover.
            snapshot = driver.snapshot()
            form = parse_captcha_form(snapshot.html or "")
            screenshot = driver.screenshot() or snapshot.screenshot
            image_path = _save_challenge(
                settings, context.job_id, (form.image if form else None) or screenshot
            )
            screen_path = _save_capture(settings, context.job_id, screenshot, "screen")
            try:
                context.park(
                    kind=walked.challenge.kind or "captcha",
                    context={
                        "kind": "portal_walk",
                        "instructions": (
                            "Type the characters in the picture into the box below "
                            "and press Submit. BidKaro carries your answer into the "
                            "portal's form once, never stores it, and continues the "
                            "walk in the same browser session."
                        ),
                        "screenshot_url": _capture_url(context.job_id, screen_path),
                    },
                    image_path=image_path,
                )
            except JobParked:
                # The session must survive the wait; the resume finds it here.
                raise
            except Exception:
                sessions.close(context.job_id)
                raise

    # A successful walk, a headless block, or a second challenge all end the
    # browser session here. The parked path above never reaches this line.
    sessions.close(context.job_id)

    with SessionLocal() as session:
        metrics = DiscoveryService(session, settings).ingest_walk(
            walked, run_id=context.run_id
        )

    notes: list[str] = []
    state = "done"
    if walked.challenge is not None:
        state = "partial"
        if headless:
            notes.append(
                "The portal asked for a human, but this browser runs headless, so "
                "there is no window to answer in. Set BROWSER_HEADLESS=false on a "
                "machine with a desktop and run discovery again."
            )
        elif run.answered_challenges > 0:
            notes.append(
                "The portal asked for a human again after the first answer. The "
                "walk stopped rather than loop on a second challenge."
            )
        else:
            notes.append(
                "The portal asked for a human before the list could be read."
            )
    elif walked.stopped_early:
        state = "partial"
        notes.append(walked.stopped_early)
    if not walked.notices and not notes:
        notes.append("The walk read no notices that passed the listing gate.")
    if walked.notices:
        notes.append(
            f"Read {len(walked.notices)} notice(s); stored "
            f"{metrics['stored']} new and updated {metrics['updated']}."
        )

    metrics.update(
        {
            "challenge": walked.challenge.to_dict() if walked.challenge else None,
            "organisations_considered": walked.organisations_considered,
            "organisations_seen": walked.organisations_seen,
        }
    )
    return JobResult(state=state, metrics=metrics, notes=" ".join(notes) or None)


def handle_sweep_portal(context) -> JobResult:
    """Read the whole portal's listings into the corpus, page by page.

    The sweep is unattended, so it never parks for a human: a challenge is
    reported and the sweep stops, because a midnight run that sits in
    ``needs_you`` would hold the browser lane until morning and block the
    user's own runs. Every page that was stored before the challenge stays
    stored, and the next run starts from the stalest organisation.
    """

    settings = get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable("Tender discovery is not enabled in this deployment.")

    counts = {"stored": 0, "updated": 0, "rejected": 0}
    with SessionLocal() as session:
        ages = organisation_ages(session)

    lane = sessions.open_lane(context.job_id, lambda: build_driver(settings))
    try:
        outcome = lane.call(
            lambda driver: _sweep_body(context, settings, driver, ages, counts)
        )
    except BrowserDriverUnavailable as exc:
        sessions.close(context.job_id)
        raise ServiceUnavailable(str(exc)) from exc
    except SessionLost:
        sessions.close(context.job_id)
        return _sweep_result(None, counts, notes=[SESSION_LOST_NOTE])
    except Exception:
        sessions.close(context.job_id)
        raise
    sessions.close(context.job_id)
    result = _sweep_result(outcome, counts)
    if result.state == "done":
        # The corpus changed, so its labels and verdicts are stale. Refresh
        # them now rather than on the next reader's request.
        _enqueue_classification(trigger="sweep")
    return result


def _sweep_body(context, settings, driver, ages, counts) -> SweepResult:
    """The sweep itself, running on the thread that owns the browser."""

    driver.begin_work()
    sweeper = PortalSweeper(
        driver,
        organisation_age=ages,
        max_organisations=settings.sweep_max_organisations,
        max_pages_per_org=settings.sweep_max_pages_per_org,
        directory_url=settings.discovery_directory_url,
        max_pages=settings.agent_walk_max_pages,
        max_org_pages=settings.agent_walk_max_org_pages,
        today=utcnow().date(),
        on_step=context.heartbeat,
    )
    return sweeper.sweep(
        lambda page: _store_sweep_page(context, settings, page, counts)
    )


def _store_sweep_page(context, settings, page, counts) -> None:
    """Store one list page and commit before the next page is read.

    This is the whole cancellation contract: a page that reached here is
    stored, so stopping between pages loses at most the page in flight.
    """

    with SessionLocal() as session:
        service = DiscoveryService(session, settings)
        if page.listing_url:
            service.record_candidate(
                run_id=context.run_id,
                url=page.listing_url,
                kind="source",
                outcome="stored",
                reason=f"Tender list for {page.organisation} was read.",
            )
        for notice in page.notices:
            tender, created, _reason = service.ingest_notice(
                notice,
                run_id=context.run_id,
                extracted_from="listing_table",
            )
            if tender is None:
                counts["rejected"] += 1
            elif created:
                counts["stored"] += 1
            else:
                counts["updated"] += 1
        session.commit()


def _sweep_result(outcome, counts, *, notes: list[str] | None = None) -> JobResult:
    metrics = {
        "stored": counts["stored"],
        "updated": counts["updated"],
        "rejected": counts["rejected"],
        "pages": getattr(outcome, "pages", 0),
        "rows_seen": getattr(outcome, "rows_seen", 0),
        "organisations_total": getattr(outcome, "organisations_total", 0),
        "organisations_walked": getattr(outcome, "organisations_walked", 0),
    }
    messages = list(notes or [])
    state = "done"
    if outcome is not None:
        metrics["errors"] = list(outcome.errors[:20])
        if outcome.challenge is not None:
            state = "partial"
            messages.append(
                "The portal asked for a human. A sweep cannot answer that, so it "
                "stopped and kept every page it had already stored."
            )
        elif outcome.stopped_early:
            state = "partial"
            messages.append(outcome.stopped_early)
        if outcome.errors:
            state = "partial"
            messages.append(
                f"{len(outcome.errors)} organisation(s) could not be read."
            )
        messages.append(
            f"Read {outcome.pages} list page(s) from {outcome.organisations_walked} "
            f"organisation(s); stored {counts['stored']} new and updated "
            f"{counts['updated']}."
        )
    else:
        metrics["errors"] = []
        state = "partial"
    return JobResult(state=state, metrics=metrics, notes=" ".join(messages) or None)


def handle_classify_corpus(context) -> JobResult:
    """Refresh the corpus intelligence tables: labels, verdicts, adjudication.

    The read path stays deterministic and fast because this job has already
    done the work. Every step is idempotent, and the model only ever sees the
    ambiguous residue, in bounded batches, with ``unknown`` shown rather than
    hidden.
    """

    settings = get_settings()
    provider = None
    try:
        from app.integrations.ai import build_ai_provider

        provider = build_ai_provider(settings)
    except Exception:  # noqa: BLE001 - a missing key must not fail classification
        provider = None

    payload = context.payload or {}
    limit = int(payload.get("limit") or 500)
    with SessionLocal() as session:
        try:
            metrics = CorpusIntelligenceService(session, settings).run(
                limit=limit, provider=provider
            )
        except Exception:
            session.rollback()
            raise

    state = "done"
    notes = [
        f"Classified {metrics['classified']} tender(s) and refreshed "
        f"{metrics['verdicts']} company verdict(s)."
    ]
    if getattr(provider, "configured", False):
        notes.append(
            f"The model judged {metrics['judged']} ambiguous tender(s); "
            f"{metrics['hidden']} were hidden and the rest were shown."
        )
    else:
        notes.append(
            "No AI provider is configured, so ambiguous rows stay visible as "
            "possible matches."
        )
    return JobResult(state=state, metrics=metrics, notes=" ".join(notes))


def handle_fetch_documents(context) -> JobResult:
    """Fetch one tender's published documents, parking for a visible captcha.

    The fetch runs on the thread that owns the browser (``sessions``): a
    resumed job finds that thread, so the human's answer and the file it
    produced are still in the same session. If the session is gone (process
    restart, closed window) the fetch starts again honestly rather than
    pretending the old page survived.
    """

    settings = get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable("Document fetching is not enabled in this deployment.")

    payload = context.payload or {}
    try:
        tender_id = uuid.UUID(str(payload["tender_id"]))
    except (KeyError, ValueError) as exc:
        raise BadRequest("This fetch job does not name a tender.") from exc
    company_id = _uuid_or_none(payload.get("company_id"))
    user_id = _uuid_or_none(payload.get("user_id"))

    run = sessions.get(context.job_id)
    fresh = run is None
    restarted = fresh and _previously_parked(context)
    if fresh:
        lane = sessions.open_lane(
            context.job_id, lambda: build_driver(settings, headless=False)
        )
        try:
            run = lane.call(
                lambda driver: sessions.FetchSession(
                    job_id=context.job_id,
                    tender_id=tender_id,
                    driver=driver,
                    created_at=time.monotonic(),
                )
            )
        except BrowserDriverUnavailable as exc:
            sessions.close(context.job_id)
            raise ServiceUnavailable(str(exc)) from exc
        except Exception:
            sessions.close(context.job_id)
            raise
        sessions.put(run, lane)
    else:
        lane = run.lane or sessions.put(run)

    try:
        result = lane.call(
            lambda driver: _fetch_body(
                context,
                settings,
                run,
                fresh,
                tender_id,
                company_id,
                user_id,
            )
        )
    except SessionLost:
        sessions.close(context.job_id)
        _mark_fetch_lost(tender_id)
        return JobResult(state="partial", notes=SESSION_LOST_NOTE)
    if lane.lost:
        # A closed window is reported once, as a partial, and never retried:
        # the page it was holding cannot come back.
        sessions.close(context.job_id)
        _mark_fetch_lost(tender_id)
        return JobResult(
            state="partial", notes=SESSION_LOST_NOTE, metrics=result.metrics
        )
    return _with_note(result, RESTARTED_NOTE) if restarted else result


def _fetch_body(
    context,
    settings,
    run,
    fresh: bool,
    tender_id: uuid.UUID,
    company_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> JobResult:
    """The fetch itself, running on the thread that owns the browser."""

    driver = run.driver
    # Small and covered while the files are being pulled. A resume comes back
    # here too, so the window returns to work mode after the human's answer.
    driver.begin_work()

    service = DocumentFetchService(
        SessionLocal(),
        settings=settings,
        company_id=company_id,
        actor_user_id=user_id,
    )
    try:
        if fresh:
            outcome = service.begin(run, on_step=context.heartbeat)
        else:
            outcome = service.resume(run, on_step=context.heartbeat)

        if outcome.challenge is not None:
            # The window stays minimized. The picture of the challenge and the
            # full page are shown in the app, where the answer is typed; the
            # software carries it into the portal's form once and drops it.
            image_path = _save_challenge(
                settings,
                context.job_id,
                outcome.challenge.image,
            )
            screen_path = _save_capture(
                settings, context.job_id, outcome.challenge.screenshot, "screen"
            )
            try:
                context.park(
                    kind=outcome.challenge.kind or "captcha",
                    context={
                        "tender_id": str(tender_id),
                        "document": str(
                            outcome.challenge.link.get("label")
                            or outcome.challenge.link.get("url")
                            or "document"
                        )[:200],
                        "instructions": (
                            outcome.challenge.message
                            + " Type the characters in the picture into the box "
                            "below and press Submit. BidKaro carries your answer "
                            "into the portal's form once, never stores it, and "
                            "continues the fetch in the same browser session."
                        ),
                        "screenshot_url": _capture_url(context.job_id, screen_path),
                    },
                    image_path=image_path,
                )
            except JobParked:
                raise
            except Exception:
                sessions.close(context.job_id)
                raise
    finally:
        # The parked path leaves the driver in the registry for the resume.
        service.session.close()
        if run is not None and run.pending_link is None:
            sessions.close(context.job_id)

    new_documents = [
        document_id
        for document_id in dict.fromkeys(outcome.documents)
        if document_id not in run.extracted
    ]
    run.extracted.update(new_documents)
    _enqueue_extraction(context, new_documents)

    return JobResult(
        state=outcome.state,
        metrics=outcome.metrics,
        notes=outcome.notes,
    )


def handle_refresh_tender(context) -> JobResult:
    """Re-read one known notice through its locator and snapshot the result.

    The notice address is session-bound, so the locator is the only stable way
    back: directory, organisation, list, row by portal tender id. A re-read
    that finds the same tender updates it and captures a snapshot; a re-read
    that finds a different tender is reported as a new row, never silently
    merged into the old one.
    """

    settings = get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable("Tender refresh is not enabled in this deployment.")

    payload = context.payload or {}
    try:
        tender_id = uuid.UUID(str(payload["tender_id"]))
    except (KeyError, ValueError) as exc:
        raise BadRequest("This refresh job does not name a tender.") from exc

    with SessionLocal() as session:
        tenders = TenderRepository(session)
        tender = tenders.get(tender_id)
        locator = tenders.locator(tender_id)
    if tender is None:
        return JobResult(state="partial", notes="The tender no longer exists.")
    if locator is None:
        return JobResult(
            state="partial",
            notes=(
                "This tender has no portal locator, so its source cannot be "
                "rechecked. Manually uploaded tenders are refreshed by uploading "
                "a newer document."
            ),
        )

    try:
        driver = build_driver(settings)
    except BrowserDriverUnavailable as exc:
        raise ServiceUnavailable(str(exc)) from exc

    # A recheck is the software's own work, so it must not take the screen
    # either. There is no handoff here: a challenge is reported and the window
    # closes.
    driver.begin_work()

    try:
        walker = PortalWalker(
            driver,
            {},
            directory_url=settings.discovery_directory_url,
            max_pages=settings.agent_walk_max_pages,
            max_org_pages=settings.agent_walk_max_org_pages,
            today=utcnow().date(),
            on_step=context.heartbeat,
        )
        notice = walker.open_tender(
            portal_tender_id=locator.portal_tender_id,
            reference=locator.reference,
            organisation_chain=locator.organisation_chain,
            work_title=locator.work_title,
        )
        challenge = driver.find_challenge()
    finally:
        driver.close()

    if notice is None:
        if challenge is not None:
            return JobResult(
                state="partial",
                notes=(
                    "The portal asked for a human before the notice could be "
                    "rechecked. Open the portal and try again."
                ),
                metrics={"challenge": challenge.to_dict()},
            )
        return JobResult(
            state="partial",
            notes=(
                "The notice could not be found again through its locator. It may "
                "have been withdrawn or moved; discovery will report it if it "
                "returns."
            ),
        )

    if notice.get("cycle_state") == "terminated":
        # The portal says this bidding cycle is over. Record the portal's own
        # sentence and stop: the old fields are not a live tender and must not
        # be shown as one.
        excerpt = str(notice.get("termination_excerpt") or "")
        with SessionLocal() as session:
            tenders = TenderRepository(session)
            stored = tenders.get(tender_id)
            if stored is not None:
                tenders.mark_terminated(
                    stored, excerpt=excerpt, observed_at=utcnow()
                )
                session.commit()
        return JobResult(
            state="done",
            metrics={
                "tender_id": str(tender_id),
                "cycle_state": "terminated",
                "termination_excerpt": excerpt,
            },
            notes=TERMINATION_MESSAGE,
        )

    with SessionLocal() as session:
        updated, created, reason = DiscoveryService(session, settings).ingest_notice(
            notice, run_id=context.run_id
        )
    if reason is not None or updated is None:
        return JobResult(
            state="partial",
            notes=reason or "The re-read notice did not pass the listing gate.",
        )

    if created:
        notes = (
            "The source now describes a different tender; it was stored as a new "
            "row and a corrigendum link was proposed for review."
        )
    else:
        notes = "The notice was re-read and a new snapshot was captured."
    return JobResult(
        state="done",
        metrics={
            "tender_id": str(updated.id),
            "created": created,
            "same_tender": updated.id == tender_id,
        },
        notes=notes,
    )


def _save_capture(settings, job_id: uuid.UUID, image: bytes | None, prefix: str) -> str | None:
    if not image:
        return None
    base = settings.storage_dir / "captures" / str(job_id)
    base.mkdir(parents=True, exist_ok=True)
    # A new name each time, so a refreshed picture is never the old one served
    # from the browser cache.
    path = base / f"{prefix}-{uuid.uuid4().hex[:8]}.png"
    path.write_bytes(image)
    return str(path)


def _save_challenge(settings, job_id: uuid.UUID, image: bytes | None) -> str | None:
    return _save_capture(settings, job_id, image, "captcha")


def _capture_url(job_id: uuid.UUID, path: str | None) -> str | None:
    if not path:
        return None
    return f"/api/jobs/{job_id}/captures/{Path(path).name}"


def _with_note(result: JobResult, note: str) -> JobResult:
    notes = " ".join(part for part in (result.notes, note) if part)
    return JobResult(state=result.state, metrics=result.metrics, notes=notes)


def _mark_fetch_lost(tender_id: uuid.UUID) -> None:
    """Record the closed window where the Documents panel reads it.

    The service writes the same note when it gets as far as finishing. This
    covers the paths that raise before that, so the panel never keeps an old,
    misleading error for a session that ended.
    """

    try:
        with SessionLocal() as session:
            documents = DocumentRepository(session).list_for_tender(
                tender_id, include_non_current=False
            )
            state = TenderAuxRepository(session).ensure_fetch_state(
                tender_id, documents_present=len(documents)
            )
            state.last_error = SESSION_LOST_NOTE
            state.last_attempt_at = utcnow()
            state.updated_at = utcnow()
            session.commit()
    except Exception:  # noqa: BLE001 - reporting must not fail the job
        pass


def _enqueue_extraction(context, document_ids: list[str]) -> None:
    if not document_ids:
        return
    from app.integrations.ai import build_ai_provider

    if not build_ai_provider().configured:
        return
    payload = context.payload or {}
    with SessionLocal() as session:
        service = JobService(session)
        for document_id in document_ids:
            service.enqueue(
                kind="extract_requirements",
                company_id=None,
                priority=60,
                payload={
                    "document_id": document_id,
                    "company_id": str(payload.get("company_id") or ""),
                    "user_id": str(payload.get("user_id") or ""),
                    "replace": False,
                },
            )


def _uuid_or_none(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _previously_parked(context) -> bool:
    """Whether this job had already waited for a human before this attempt."""

    latest = getattr(context, "latest_challenge", None)
    if latest is None:
        return False
    try:
        return latest() is not None
    except Exception:  # noqa: BLE001 - a lookup must not stop the job
        return False


def _enqueue_classification(*, trigger: str) -> None:
    """Queue the corpus-intelligence pass after a sweep.

    Labels and verdicts are what make the read path cheap, so they are
    refreshed as soon as the corpus has changed. The job is idempotent, so an
    extra enqueue costs a pass over already-stamped rows and nothing else.
    """

    from app.services.job_registry import get_handlers

    if "classify_corpus" not in get_handlers():
        return
    try:
        with SessionLocal() as session:
            JobService(session).enqueue(
                kind="classify_corpus",
                company_id=None,
                priority=80,
                payload={"trigger": trigger},
            )
    except Exception:  # noqa: BLE001 - a failed enqueue must not fail a sweep
        pass


def start_fetch_on_bidding(
    tender_id: uuid.UUID, company_id: uuid.UUID, actor_user_id: uuid.UUID | None
) -> None:
    with SessionLocal() as session:
        enqueue_fetch(
            session,
            tender_id=tender_id,
            company_id=company_id,
            user_id=actor_user_id,
        )


def close_job_session(kind: str, job_id: uuid.UUID) -> None:
    if kind in {"fetch_documents", "scrape_portal", "sweep_portal"}:
        sessions.close(job_id)


def register_discovery() -> None:
    register_handler("scrape_portal", handle_scrape_portal)
    register_handler("sweep_portal", handle_sweep_portal)
    register_handler("classify_corpus", handle_classify_corpus)
    register_handler("fetch_documents", handle_fetch_documents)
    register_handler("refresh_tender", handle_refresh_tender)
    register_job_closed(close_job_session)
    register_bidding_fetch(start_fetch_on_bidding)
