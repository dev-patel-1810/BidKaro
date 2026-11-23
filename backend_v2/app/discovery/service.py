"""Discovery ingestion, read-time listing and run status.

The browser walk produces plain dictionaries. This module turns each read into
one global tender row, records every URL the run saw (including rejections),
and exposes the run/status surface the frontend polls.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict, ServiceUnavailable
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.discovery import tender_extraction as extraction
from app.discovery.relevance import (
    company_profile_dict,
    company_terms,
    understanding_terms_from_rows,
)
from app.discovery.portal_walk import WalkResult
from app.models.corpus import DiscoveryCandidate, Portal, Tender
from app.models.identity import Company, User
from app.models.work import Job
from app.repositories.corpus import TenderRepository
from app.repositories.intelligence import ObservationRepository
from app.repositories.understanding import CompanyUnderstandingRepository
from app.repositories.work import RunRepository
from app.services.jobs import JobService
from app.services.workflow import WorkflowService

CPPP_CODE = "cppp"
SWEEP_KIND = "sweep_portal"
RUN_KIND = "scrape_portal"
CLASSIFY_KIND = "classify_corpus"
_SAFE_ID = re.compile(r"[^a-z0-9]+")


def _norm(value: str | None) -> str:
    return _SAFE_ID.sub("", (value or "").casefold())


def _noon(value: date) -> datetime:
    return datetime.combine(value, time(12, 0), tzinfo=timezone.utc)


def _first_date(value: str | None) -> date | None:
    if not value:
        return None
    direct = extraction.parse_date_token(value)
    if direct is not None:
        return direct
    for match in re.finditer(
        r"\d{1,2}[-/.\s][A-Za-z0-9]{2,9}[-/.\s]\d{2,4}"
        r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}",
        value,
    ):
        parsed = extraction.parse_date_token(match.group(0))
        if parsed is not None:
            return parsed
    return None


def _closing_date(value: str | None, *, today: date) -> date | None:
    if not value:
        return None
    facts = extraction.read_close_date(value, today=today)
    if facts.deadline is not None:
        return facts.deadline
    return _first_date(value)


def _amount(value: str | None) -> Decimal | None:
    if not value:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def dedupe_key_for(
    *,
    portal_code: str,
    portal_tender_id: str | None,
    reference: str | None,
    work_title: str | None,
    organisation: str | None,
) -> str:
    """The deterministic fallback identity, with no dates in it."""

    if portal_tender_id:
        base = f"portal:{portal_code}:{_norm(portal_tender_id)}"
    else:
        base = "|".join(
            part
            for part in (
                portal_code,
                _norm(reference),
                _norm(work_title),
                _norm(organisation),
            )
            if part
        )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


class DiscoveryService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.tenders = TenderRepository(session)
        self.runs = RunRepository(session)
        self.jobs = JobService(session, self.settings)

    # -- status / runs ----------------------------------------------------

    def _portal(self) -> Portal:
        portal = self.session.scalar(select(Portal).where(Portal.code == CPPP_CODE))
        if portal is None:
            portal = Portal(
                code=CPPP_CODE,
                name="Central Public Procurement Portal",
                base_url="https://eprocure.gov.in",
            )
            self.session.add(portal)
            self.session.flush()
        return portal

    def status(self) -> dict[str, Any]:
        last_run = self.session.scalar(
            select(Job)
            .where(Job.kind == "scrape_portal")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        last_completed = self.jobs.last_completed_run_at()
        scraper_due = self.jobs.scheduler_due()
        tender_count = int(
            self.session.scalar(
                select(func.count(Tender.id)).where(Tender.deleted_at.is_(None))
            )
            or 0
        )
        source_count = int(
            self.session.scalar(
                select(func.count(func.distinct(DiscoveryCandidate.url))).where(
                    DiscoveryCandidate.kind == "source"
                )
            )
            or 0
        )
        return {
            "enabled": self.settings.discovery_enabled,
            "last_run": last_run,
            "last_completed_run_at": last_completed,
            "due": scraper_due,
            "tender_count": tender_count,
            "source_count": source_count,
            "sweep": self.sweep_status(),
            "caps": {
                "max_notices": self.settings.agent_walk_max_notices,
                "max_notices_per_org": self.settings.agent_walk_max_notices_per_org,
                "max_pages": self.settings.agent_walk_max_pages,
                "max_org_pages": self.settings.agent_walk_max_org_pages,
                "max_tenders": self.settings.discovery_max_tenders,
                "interval_hours": self.settings.scrape_interval_hours,
            },
        }

    def list_sources(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(
                DiscoveryCandidate.url,
                func.max(DiscoveryCandidate.last_seen_at),
                func.count(DiscoveryCandidate.id),
                func.max(DiscoveryCandidate.reason),
            )
            .where(DiscoveryCandidate.kind == "source")
            .group_by(DiscoveryCandidate.url)
            .order_by(func.max(DiscoveryCandidate.last_seen_at).desc())
            .limit(limit)
        ).all()
        return [
            {
                "url": row[0],
                "last_seen_at": row[1],
                "seen_count": row[2],
                "reason": row[3],
            }
            for row in rows
        ]

    def list_runs(
        self, company_id: uuid.UUID, *, limit: int = 50
    ) -> list[Job]:
        return self.jobs.list_visible(
            company_id, kind="scrape_portal", limit=limit
        )

    def get_run(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        job = self.jobs.get_visible(job_id, company_id)
        if job.kind != "scrape_portal":
            raise BadRequest("That job is not a discovery run.")
        return job

    def cancel_run(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        job = self.get_run(job_id, company_id)
        return self.jobs.cancel(job.id, company_id)

    # -- sweeps -----------------------------------------------------------

    def list_sweeps(
        self, company_id: uuid.UUID, *, limit: int = 20
    ) -> list[Job]:
        return self.jobs.list_visible(company_id, kind=SWEEP_KIND, limit=limit)

    def get_sweep(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        job = self.jobs.get_visible(job_id, company_id)
        if job.kind != SWEEP_KIND:
            raise BadRequest("That job is not a portal sweep.")
        return job

    def cancel_sweep(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        """Ask a sweep to stop at the next page boundary.

        The worker checks the row on every step, so the sweep stops before it
        reads another page or opens another organisation. Everything already
        stored stays stored: a cancelled sweep is progress, not a rollback.
        """

        job = self.get_sweep(job_id, company_id)
        return self.jobs.cancel(job.id, company_id)

    def start_sweep(
        self, *, company: Company, user: User
    ) -> tuple[Job, bool]:
        if not self.settings.discovery_enabled:
            raise ServiceUnavailable(
                "Tender discovery is not installed in this deployment."
            )
        active = self.jobs.list_visible(
            company.id,
            states=["queued", "running", "needs_you"],
            kind=SWEEP_KIND,
            limit=1,
        )
        if active:
            return active[0], False
        if self.jobs.list_visible(
            company.id,
            states=["queued", "running", "needs_you"],
            kind=RUN_KIND,
            limit=1,
        ):
            # One browser at a time. Two runs would fight over the same portal
            # session and both would see half of it.
            raise Conflict(
                "Run discovery is using the browser right now. Wait for it to "
                "finish or cancel it, then start the sweep."
            )
        job, created = self.jobs.enqueue(
            kind=SWEEP_KIND,
            company_id=company.id,
            lane="browser",
            # Below an interactive run: the sweep is a background chore and
            # must not delay work the user is watching.
            priority=30,
            payload={"requested_by": str(user.id), "trigger": "manual"},
        )
        return job, created

    def sweep_status(self) -> dict[str, Any]:
        last = self.session.scalar(
            select(Job)
            .where(Job.kind == SWEEP_KIND)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        summary = None
        if last is not None:
            # A sweep returns counts, not just a state. The last read is what
            # the page shows, so "swept at 2am, 41 pages, 312 rows, 96 stored"
            # is visible without opening a log.
            latest = self.runs.list_for_job(last.id)
            run = latest[0] if latest else None
            if run is not None:
                summary = {
                    "state": run.status,
                    "metrics": run.metrics or {},
                    "notes": run.notes,
                    "finished_at": run.finished_at,
                }
        return {
            "last_sweep": last,
            "last_summary": summary,
            "due": self.jobs.scheduler_due(),
            "caps": {
                "max_organisations": self.settings.sweep_max_organisations,
                "max_pages_per_org": self.settings.sweep_max_pages_per_org,
                "interval_hours": self.settings.scrape_interval_hours,
            },
        }

    # -- corpus intelligence ----------------------------------------------

    def start_classification(self, *, user: User) -> tuple[Job, bool]:
        """Queue the labelling and verdict pass.

        It is safe to ask for this at any time: every step is idempotent, and
        a row that is already stamped is skipped. A second request while one
        is queued returns the queued job rather than stacking duplicates.
        """

        if not self.settings.discovery_enabled:
            raise ServiceUnavailable(
                "Tender discovery is not installed in this deployment."
            )
        active = self.jobs.list_visible(
            None,
            states=["queued", "running"],
            kind=CLASSIFY_KIND,
            limit=1,
        )
        if active:
            return active[0], False
        return self.jobs.enqueue(
            kind=CLASSIFY_KIND,
            company_id=None,
            priority=70,
            payload={"requested_by": str(user.id), "trigger": "manual"},
        )

    def classification_status(self) -> dict[str, Any]:
        last = self.session.scalar(
            select(Job)
            .where(Job.kind == CLASSIFY_KIND)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        return {
            "last_classification": last,
            "batch_size": self.settings.classification_batch_size,
            "max_batches": self.settings.classification_max_batches,
        }

    def start_run(
        self, *, company: Company, user: User
    ) -> tuple[Job, bool]:
        if not self.settings.discovery_enabled:
            raise ServiceUnavailable(
                "Tender discovery is not installed in this deployment."
            )
        companies = list(
            self.session.scalars(
                select(Company).where(Company.is_active.is_(True))
            ).all()
        )
        has_work_terms = False
        understanding = CompanyUnderstandingRepository(self.session)
        for row in companies:
            terms = company_terms(
                company_profile_dict(
                    row,
                    understanding_terms=understanding_terms_from_rows(
                        understanding.confirmed_capabilities(row.id),
                        understanding.confirmed_experiences(row.id),
                    ),
                )
            )
            if (
                terms["understanding"]
                or terms["keywords"]
                or terms["capability"]
            ):
                has_work_terms = True
                break
        if not has_work_terms:
            raise BadRequest(
                "Add at least one sector, service or keyword, or confirm a "
                "capability from your company profile, before running discovery."
            )

        active = self.jobs.list_visible(
            company.id,
            states=["queued", "running", "needs_you"],
            kind=RUN_KIND,
            limit=1,
        )
        if active:
            return active[0], False
        if self.jobs.list_visible(
            company.id,
            states=["queued", "running", "needs_you"],
            kind=SWEEP_KIND,
            limit=1,
        ):
            raise Conflict(
                "A portal sweep is using the browser right now. Wait for it to "
                "finish or stop it, then run discovery."
            )

        job, created = self.jobs.enqueue(
            kind="scrape_portal",
            company_id=company.id,
            lane="browser",
            priority=50,
            payload={"requested_by": str(user.id)},
        )
        return job, created

    # -- ingestion --------------------------------------------------------

    def record_candidate(
        self,
        *,
        run_id: uuid.UUID | None,
        url: str,
        kind: str,
        outcome: str,
        reason: str | None = None,
        listing_url: str | None = None,
    ) -> None:
        if run_id is None or not url:
            return
        existing = self.session.scalar(
            select(DiscoveryCandidate).where(
                DiscoveryCandidate.run_id == run_id,
                DiscoveryCandidate.url == url,
                DiscoveryCandidate.kind == kind,
            )
        )
        now = utcnow()
        if existing is None:
            self.session.add(
                DiscoveryCandidate(
                    run_id=run_id,
                    url=url[:4000],
                    kind=kind,
                    outcome=outcome,
                    reason=(reason or None) and reason[:1000],
                    listing_url=(listing_url or None) and listing_url[:4000],
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            existing.outcome = outcome
            existing.reason = (reason or None) and reason[:1000]
            existing.listing_url = (listing_url or None) and listing_url[:4000]
            existing.last_seen_at = now
        self.session.flush()

    def _values_for_notice(
        self,
        notice: dict[str, Any],
        portal: Portal,
        *,
        extracted_from: str = "notice_page",
    ) -> tuple[dict[str, Any] | None, str | None]:
        today = utcnow().date()
        title = extraction.normalize_text(notice.get("title") or "")
        if not title or extraction.is_listing_page(title):
            return None, "The page was a category or index, not one work."
        page_text = notice.get("page_text") or ""
        if extracted_from != "listing_table" and extraction.is_error_page(page_text):
            return None, "The page was an error page."

        tender_id = extraction.normalize_tender_id(notice.get("tender_id"))
        reference = extraction.normalize_text(notice.get("reference_number") or "")
        closing = _closing_date(notice.get("closing_date"), today=today)
        if closing is None:
            return None, "No closing date could be read."
        if closing < today:
            return None, "The closing date is in the past."
        published = _first_date(notice.get("publishing_date"))

        authority = extraction.normalize_text(notice.get("authority") or "") or None
        organisation = (
            extraction.normalize_text(notice.get("organisation_chain") or "") or None
        )
        location = extraction.normalize_text(notice.get("location") or "") or None
        value = _amount(notice.get("value"))

        dedupe_key = dedupe_key_for(
            portal_code=portal.code,
            portal_tender_id=tender_id,
            reference=reference,
            work_title=title,
            organisation=organisation or authority,
        )
        raw = {
            "notice": {
                key: notice.get(key)
                for key in (
                    "tender_id",
                    "reference_number",
                    "closing_date",
                    "publishing_date",
                    "opening_date",
                    "location",
                    "value",
                    "bid_validity",
                )
            },
            "evidence": notice.get("extracted_evidence") or {},
            "document_links": (notice.get("document_links") or [])[:50],
            "page_text_sha256": hashlib.sha256(
                page_text.encode("utf-8")
            ).hexdigest(),
            "page_text_excerpt": page_text[:20_000],
        }
        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "title": title,
                    "reference": reference,
                    "closing": closing.isoformat(),
                    "published": published.isoformat() if published else None,
                    "value": str(value) if value is not None else None,
                    "location": location,
                    "organisation": organisation,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        values = {
            "portal_id": portal.id,
            "portal_tender_id": tender_id,
            "reference": reference or None,
            "reference_normalised": _norm(reference) or None,
            "authority": authority,
            "organisation_chain": organisation,
            "work_title": title[:400],
            "work_type": None,
            "location": location,
            "region": location,
            "closing_at": _noon(closing),
            "published_at": _noon(published) if published else None,
            "value_amount": value,
            "currency": "INR",
            "source_type": "official_portal",
            "extracted_from": extracted_from,
            "source_url": (notice.get("notice_url") or "")[:4000] or None,
            "retrieved_at": utcnow(),
            "content_hash": content_hash,
            "unverified": (None if extracted_from == "listing_table" else []),
            "raw": raw,
        }
        return values, None

    def _observe(
        self,
        notice: dict[str, Any],
        *,
        outcome: str,
        tender_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        """Write one sighting to the audit trail.

        This is a record of what the run actually saw, so it is written from
        the notice as read: the organisation it was found under, the row's
        tender id and reference, and the address. The address is kept as
        evidence but the route key is built from the durable fields, because a
        portal notice address stops working when the session ends.
        """

        organisation = extraction.normalize_text(
            notice.get("authority")
            or notice.get("organisation_chain")
            or notice.get("organisation")
            or ""
        )
        portal_tender_id = extraction.normalize_tender_id(notice.get("tender_id"))
        reference = extraction.normalize_text(notice.get("reference_number") or "")
        title = extraction.normalize_text(notice.get("title") or "")
        route_key = " | ".join(
            part
            for part in (organisation, portal_tender_id or reference or title)
            if part
        )
        ObservationRepository(self.session).record(
            route_key=route_key or title or "unknown",
            outcome=outcome,
            tender_id=tender_id,
            run_id=run_id,
            organisation=organisation or None,
            listing_url=notice.get("listing_url"),
            portal_tender_id=portal_tender_id,
            reference=reference or None,
            href=notice.get("notice_url"),
            reason=reason,
        )

    def ingest_notice(
        self,
        notice: dict[str, Any],
        *,
        run_id: uuid.UUID | None = None,
        today: date | None = None,
        extracted_from: str = "notice_page",
    ) -> tuple[Tender | None, bool, str | None]:
        portal = self._portal()
        values, reason = self._values_for_notice(
            notice, portal, extracted_from=extracted_from
        )
        if values is None:
            self.record_candidate(
                run_id=run_id,
                url=str(notice.get("notice_url") or notice.get("title") or "unknown"),
                kind="tender_candidate",
                outcome="rejected",
                reason=reason,
                listing_url=notice.get("listing_url"),
            )
            self._observe(notice, outcome="rejected", run_id=run_id, reason=reason)
            self.session.commit()
            return None, False, reason

        dedupe_key = str(
            dedupe_key_for(
                portal_code=portal.code,
                portal_tender_id=values.get("portal_tender_id"),
                reference=values.get("reference"),
                work_title=values.get("work_title"),
                organisation=values.get("organisation_chain")
                or values.get("authority"),
            )
        )
        try:
            tender, created = self.tenders.upsert(values, dedupe_key=dedupe_key)
            self.tenders.upsert_locator(
                tender.id,
                {
                    "portal_id": portal.id,
                    "portal_tender_id": values.get("portal_tender_id"),
                    "reference": values.get("reference"),
                    "organisation_chain": values.get("organisation_chain"),
                    "work_title": values.get("work_title"),
                    "published_on": (
                        values["published_at"].date()
                        if values.get("published_at") is not None
                        else None
                    ),
                    "last_verified_at": utcnow(),
                },
            )
            # A new read may be a corrigendum. It is only ever a proposal;
            # a human confirms or rejects it later.
            WorkflowService(self.session).propose_lineage(
                tender.id,
                company_id=None,
                actor_user_id=None,
                commit=False,
            )
            WorkflowService(self.session).capture_snapshot(
                tender.id,
                company_id=None,
                actor_user_id=None,
                commit=False,
            )
            self.record_candidate(
                run_id=run_id,
                url=str(notice.get("notice_url") or dedupe_key),
                kind="tender_candidate",
                outcome="stored",
                reason=None,
                listing_url=notice.get("listing_url"),
            )
            self._observe(
                notice,
                outcome="stored" if created else "updated",
                tender_id=tender.id,
                run_id=run_id,
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return tender, created, None

    def ingest_walk(
        self,
        result: WalkResult,
        *,
        run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        for candidate in result.candidates:
            self.record_candidate(
                run_id=run_id,
                url=str(candidate.get("url") or ""),
                kind=str(candidate.get("kind") or "tender_candidate"),
                outcome=str(candidate.get("outcome") or "rejected"),
                reason=candidate.get("reason"),
                listing_url=candidate.get("listing_url"),
            )
            # A stored notice is recorded with its tender id by
            # ``ingest_notice``; the candidate row is only the page-level
            # summary. Everything else - a row rejected by the gate, a notice
            # whose page would not open, a terminal bidding cycle - is a
            # sighting in its own right and belongs in the trail.
            if str(candidate.get("kind") or "tender_candidate") != "tender_candidate":
                continue
            outcome = str(candidate.get("outcome") or "rejected")
            if outcome == "stored":
                continue
            reason = candidate.get("reason")
            if outcome == "rejected" and reason and "terminated" in reason.lower():
                outcome = "terminated"
            if outcome in ("rejected", "unreadable", "terminated", "missing"):
                self._observe(
                    candidate,
                    outcome=outcome,
                    run_id=run_id,
                    reason=reason,
                )
        self.session.commit()

        stored = 0
        updated = 0
        rejected = 0
        for notice in result.notices:
            tender, created, reason = self.ingest_notice(notice, run_id=run_id)
            if tender is None:
                rejected += 1
            elif created:
                stored += 1
            else:
                updated += 1
        return {
            "stored": stored,
            "updated": updated,
            "rejected": rejected,
            "rows_seen": result.rows_seen,
            "pages_walked": result.pages_walked,
            "shortlisted": result.shortlisted,
            "notices_read": len(result.notices),
            "terminated": result.terminated,
            "unreadable": self._unreadable(result),
            "organisations": [
                {
                    "name": row.name,
                    "rows_seen": row.rows_seen,
                    "shortlisted": row.shortlisted,
                    "notices": row.notices,
                    "stopped": row.stopped,
                }
                for row in result.organisations
            ],
            "stopped_early": result.stopped_early,
        }

    @staticmethod
    def _unreadable(result: WalkResult) -> int:
        return sum(
            1
            for item in result.candidates
            if item.get("kind") == "tender_candidate"
            and item.get("outcome") == "unreadable"
        )


def union_profile(
    companies: list[Company],
    understanding_terms: dict[uuid.UUID, list[str]] | None = None,
) -> dict[str, Any]:
    """The crawl target is the union of the active company profiles.

    The corpus is global. The union only decides which organisations and list
    rows are worth spending a notice on; it is never used as a storage filter.
    Only human-confirmed capability/experience phrases join the declared
    profile. Raw words harvested from an about-company PDF are not included:
    they are evidence for a human to confirm, not a work claim.
    """

    understanding_terms = understanding_terms or {}
    merged: dict[str, Any] = {
        "products_services": [],
        "sectors": [],
        "preferences": {
            "keywords": [],
            "sectors": [],
            "regions": [],
            "understanding": [],
        },
        "regions": [],
        "understanding": [],
    }
    for company in companies:
        profile = company_profile_dict(
            company, understanding_terms=understanding_terms.get(company.id)
        )
        for key in ("products_services", "sectors", "regions", "understanding"):
            merged[key].extend(profile.get(key) or [])
        prefs = profile.get("preferences") or {}
        for key in ("keywords", "sectors", "regions", "understanding"):
            merged["preferences"][key].extend(prefs.get(key) or [])
    for key in ("products_services", "sectors", "regions", "understanding"):
        merged[key] = list(dict.fromkeys(merged[key]))
    for key in ("keywords", "sectors", "regions", "understanding"):
        merged["preferences"][key] = list(
            dict.fromkeys(merged["preferences"][key])
        )
    return merged

