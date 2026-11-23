"""Document acquisition: a deterministic walk to the files behind a notice.

The browser session is the unit of work. A captcha is the portal's response to
a file request, so the file request itself is read as a page: the software
opens it, recognises the portal's own captcha form, and hands the picture to
the user in the app. The user types the answer there, the software carries it
into the portal's form once and drops it, and the file the portal returns is
attached.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.discovery import tender_extraction as extraction
from app.discovery.browser import SESSION_LOST_NOTE, DownloadResult
from app.discovery.captcha import parse_captcha_form
from app.discovery.portal_walk import PortalWalker
from app.discovery.sessions import FetchSession
from app.models.corpus import Tender
from app.repositories.corpus import (
    DocumentRepository,
    TenderAuxRepository,
    TenderRepository,
)
from app.repositories.work import JobRepository
from app.services.document_service import DocumentService
from app.services.job_types import JobCancelled
from app.services.jobs import JobService

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "document",
    "documents",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
_COVER_STOP = {
    "tender",
    "details",
    "detail",
    "document",
    "documents",
    "cover",
    "no",
    "s",
    "serial",
}
_COVER_HEADER = re.compile(r"\bcover\b", re.IGNORECASE)
_COVER_END = re.compile(
    r"^(tender documents?|tender fee|emd fee|document download|other details|"
    r"critical dates|bid submission)",
    re.IGNORECASE,
)
_COVER_ROW = re.compile(r"^\s*(\d{1,3})\s*[|\t ]+\s*(\d{1,3})\s*[|\t ]+(.+?)\s*$")
# The portal's cover names are a small, stable vocabulary. When a row is a
# single flattened cell, the name still marks where the type ends and the
# description begins, so the type is not read as the whole line.
_COVER_TYPES = (
    "tender notice",
    "tender document",
    "bill of quantities",
    "integrity pact",
    "technical bid",
    "financial bid",
    "price bid",
    "drawings",
    "boq",
    "rfp",
    "emd",
)


#: A wrong captcha answer is retried in the app, with the portal's new picture,
#: but a portal that never accepts an answer is reported rather than looped on.
_MAX_REJECTED_ANSWERS = 3

#: One document fetch may show this many pictures in total. A portal that keeps
#: asking after a correct answer is reported instead of making the user answer
#: pictures forever; the remaining files can be uploaded manually.
_MAX_CHALLENGES_PER_FETCH = 6

#: Follow at most this many links from the page an accepted answer produced.
#: A page after the answer is the portal's own next step, not the original
#: captcha request, so it is read here instead of requesting that URL again.
_MAX_RECOVERY_LINKS = 4


@dataclass(slots=True)
class FetchChallenge:
    kind: str
    message: str
    image: bytes | None
    link: dict[str, Any]
    screenshot: bytes | None = None


@dataclass(slots=True)
class FetchOutcome:
    state: str
    documents: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    challenge: FetchChallenge | None = None
    notes: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _split_cover(rest: str) -> tuple[str, str]:
    """Split a flattened cover cell into its type and description."""

    parts = [part.strip() for part in re.split(r"\s{2,}|\t|\|", rest) if part.strip()]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    folded = rest.casefold()
    for known in _COVER_TYPES:
        if folded == known:
            return rest, rest
        if folded.startswith(known + " "):
            return rest[: len(known)].strip(), rest[len(known) :].strip() or rest
    return rest, rest


def parse_covers(text: str) -> list[dict[str, Any]]:
    """Read the portal's own cover table.

    The table is an expected-document list, not an arbiter of done. A cover
    may have no downloadable file at all, and five covers may arrive as one
    zip; both are reported, never turned into a hard failure.
    """

    lines = [line.strip() for line in (text or "").splitlines()]
    start = None
    for index, line in enumerate(lines):
        if _COVER_HEADER.search(line) and (
            "cover no" in line.casefold()
            or "document type" in line.casefold()
            or line.casefold().strip() in {"cover details", "covers"}
        ):
            start = index
            break
    if start is None:
        return []

    rows: list[dict[str, Any]] = []
    for line in lines[start + 1 :]:
        if not line:
            continue
        if _COVER_END.search(line):
            break
        match = _COVER_ROW.match(line)
        if match is None:
            # A header line such as "S.No Cover No Document Type Description".
            continue
        cover_no = int(match.group(2))
        rest = match.group(3).strip()
        doc_type, description = _split_cover(rest)
        if cover_no <= 0:
            continue
        rows.append(
            {
                "cover_no": cover_no,
                "doc_type": doc_type[:200] or None,
                "description": description[:500] or None,
            }
        )
    return rows


def _cover_terms(cover) -> list[str]:
    text = f"{cover.doc_type or ''} {cover.description or ''}"
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return [
        word
        for word in words
        if len(word) >= 3 and word not in _STOPWORDS and word not in _COVER_STOP
    ]


def missing_covers(covers, documents: list[Any]) -> list[int]:
    """Covers with no file that even looks like them.

    Matching is deliberately generous: a cover is only called missing when it
    has at least one meaningful word and that word is absent from every
    document name. When there are at least as many files as covers, the set is
    complete; extra files are normal.
    """

    if not covers or len(documents) >= len(covers):
        return []
    names = [" ".join(filter(None, [doc.filename, doc.document_key])) for doc in documents]
    haystack = " ".join(names).casefold()
    missing: list[int] = []
    for cover in covers:
        terms = _cover_terms(cover)
        if not terms:
            continue
        if not any(term in haystack for term in terms):
            missing.append(int(cover.cover_no))
    return sorted(set(missing))


def _merge_links(stored: list[dict], fresh: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for link in [*fresh, *stored]:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        kind = str(link.get("kind") or "unknown")
        merged.append(
            {
                **link,
                "url": url,
                "label": str(link.get("label") or "")[:200],
                "kind": kind if kind in {"zip", "pdf", "file", "unknown"} else "unknown",
            }
        )
    return merged


def _ordered_links(links: list[dict]) -> list[dict]:
    # The individual files come first and the bundle is the fallback: a portal
    # that hands over each PDF on its own does not need the zip, and asking for
    # it anyway costs a second captcha and attaches the same files twice.
    rank = {"pdf": 0, "file": 1, "unknown": 2, "zip": 3}
    return sorted(links, key=lambda link: rank.get(str(link.get("kind")), 2))


def _looks_like_file_url(url: str) -> bool:
    """Whether a page's own address can be the file the answer produced.

    The portal's document component and a direct file name both count; the
    bare application URL does not, because fetching it would draw the captcha
    page again.
    """

    folded = (url or "").casefold()
    return any(
        marker in folded
        for marker in (
            ".pdf",
            ".zip",
            ".xls",
            ".xlsx",
            ".doc",
            ".docx",
            "docdownoad",
            "directlink_",
        )
    )


class DocumentFetchService:
    """Fetch a tender's published documents in one browser session."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.company_id = company_id
        self.actor_user_id = actor_user_id
        self.tenders = TenderRepository(session)
        self.aux = TenderAuxRepository(session)
        self.documents = DocumentService(session, self.settings)
        self.jobs = JobService(session, self.settings)

    # -- entry points -----------------------------------------------------

    def begin(
        self,
        run: FetchSession,
        *,
        on_step: Callable[[], None] | None = None,
    ) -> FetchOutcome:
        self._current_tender_id = run.tender_id
        tender = self.tenders.get(run.tender_id)
        if tender is None:
            return self._finish(
                run,
                FetchOutcome(
                    state="partial",
                    notes="The tender no longer exists.",
                    errors=["The tender no longer exists."],
                ),
            )
        if (tender.cycle_state or "active") == "terminated":
            return self._finish(
                run,
                FetchOutcome(
                    state="partial",
                    notes=extraction.TERMINATION_MESSAGE,
                    errors=[extraction.TERMINATION_MESSAGE],
                ),
            )

        opened = self._open_notice(run, tender, on_step=on_step)
        if opened is None:
            return self._finish(
                run,
                FetchOutcome(
                    state="partial",
                    notes=(
                        "The portal could not be reached to find this notice again. "
                        "Run discovery again or upload the documents manually."
                    ),
                    errors=[
                        "The notice could not be opened; there is no document link to fetch."
                    ],
                ),
            )
        snapshot, links = opened
        statement = extraction.termination_statement(snapshot.text or "")
        if statement is not None:
            self.tenders.mark_terminated(
                tender, excerpt=statement, observed_at=utcnow()
            )
            return self._finish(
                run,
                FetchOutcome(
                    state="partial",
                    notes=extraction.TERMINATION_MESSAGE,
                    errors=[extraction.TERMINATION_MESSAGE],
                ),
            )
        if not links:
            return self._finish(
                run,
                FetchOutcome(
                    state="partial",
                    notes="This notice did not carry any document links.",
                    errors=["The notice did not carry any document links."],
                ),
            )

        covers = parse_covers(snapshot.text or "")
        self._store_covers(tender.id, covers)
        run.links = _ordered_links(links)
        run.index = 0
        return self._process(run, on_step=on_step)

    def resume(
        self,
        run: FetchSession,
        *,
        on_step: Callable[[], None] | None = None,
    ) -> FetchOutcome:
        self._current_tender_id = run.tender_id
        if run.pending_link is not None:
            challenge = self._consume_pending(run)
            if challenge is not None:
                return self._finish(
                    run,
                    FetchOutcome(
                        state="partial",
                        documents=list(run.documents),
                        errors=list(run.errors),
                        challenge=challenge,
                        notes=challenge.message,
                        metrics=self._metrics(run),
                    ),
                    challenge_pending=True,
                )
        return self._process(run, on_step=on_step)

    # -- locating the notice ----------------------------------------------

    def _open_notice(
        self,
        run: FetchSession,
        tender: Tender,
        *,
        on_step: Callable[[], None] | None = None,
    ) -> tuple[Any, list[dict]] | None:
        page = getattr(run.driver, "_page", None)
        if page is None:
            return None
        snapshot = None
        notice: dict | None = None

        # A source URL is a session-bound address. It is worth one try when
        # the fetch starts minutes after the walk, but it is never the locator.
        if tender.source_url:
            try:
                candidate = run.driver.open(tender.source_url)
            except Exception:  # noqa: BLE001
                candidate = None
            if (
                candidate is not None
                and not candidate.is_blank
                and not extraction.is_error_page(candidate.text or "")
            ):
                snapshot = candidate

        if snapshot is None:
            locator = self.tenders.locator(tender.id)
            if locator is None:
                return None
            walker = PortalWalker(
                run.driver,
                {},
                directory_url=self.settings.discovery_directory_url,
                max_pages=self.settings.agent_walk_max_pages,
                max_org_pages=self.settings.agent_walk_max_org_pages,
                today=utcnow().date(),
                on_step=on_step,
            )
            notice = walker.open_tender(
                portal_tender_id=locator.portal_tender_id,
                reference=locator.reference,
                organisation_chain=locator.organisation_chain,
                work_title=locator.work_title,
            )
            if notice is None:
                return None
            snapshot = run.driver.snapshot()
        if snapshot is None:
            return None
        if extraction.is_error_page(snapshot.text or ""):
            return None

        raw = tender.raw or {}
        stored = raw.get("document_links") if isinstance(raw, dict) else []
        fresh = list(notice.get("document_links") or []) if notice is not None else []
        if not fresh:
            fresh = PortalWalker._document_links(page)
        links = _merge_links(
            list(stored or []) if isinstance(stored, list) else [],
            fresh,
        )
        base_url = snapshot.url or tender.source_url or ""
        for link in links:
            link["url"] = urljoin(base_url, link["url"])
        return snapshot, links

    # -- fetching ---------------------------------------------------------

    def _process(
        self,
        run: FetchSession,
        *,
        on_step: Callable[[], None] | None = None,
    ) -> FetchOutcome:
        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        while run.index < len(run.links):
            if on_step is not None:
                try:
                    on_step()
                except JobCancelled:
                    # A cancelled fetch must stop now, not after the last file.
                    raise
                except Exception:  # noqa: BLE001 - a heartbeat must not stop a fetch
                    pass
            link = run.links[run.index]
            url = str(link.get("url") or "").strip()
            label = str(link.get("label") or "").strip() or "document"
            if not url:
                run.index += 1
                continue
            if str(link.get("kind") or "") == "zip" and run.documents:
                # The PDFs arrived on their own, so the bundle is not needed.
                # Fetching it anyway would ask for a second captcha and attach
                # the same files again.
                run.index += 1
                continue

            result = run.driver.download_in_page(url, max_bytes=max_bytes)
            if result.is_page:
                challenge = self._challenge_from_page(run, link, result)
                if challenge is not None:
                    return self._finish(
                        run,
                        FetchOutcome(
                            state="partial",
                            documents=list(run.documents),
                            errors=list(run.errors),
                            challenge=challenge,
                            notes=(
                                "The portal asked for a captcha before handing over a "
                                "document. Answer it in the portal window, then press "
                                "Continue."
                            ),
                            metrics=self._metrics(run),
                        ),
                        challenge_pending=True,
                    )
                run.errors.append(
                    f"{label}: the portal answered with a page, not a document."
                )
                run.index += 1
                continue
            if not result.ok:
                run.errors.append(
                    f"{label}: {result.error or 'the download failed.'}"
                )
                run.index += 1
                continue
            added, problems = self._attach_content(
                content=result.content,
                label=label,
                source_url=url,
                max_bytes=max_bytes,
            )
            run.documents.extend(added)
            run.errors.extend(problems)
            run.index += 1

        return self._finish(
            run,
            FetchOutcome(
                state="done" if not run.errors else "partial",
                documents=list(dict.fromkeys(run.documents)),
                errors=list(run.errors),
                notes=self._done_note(run),
                metrics=self._metrics(run),
            ),
        )

    def _challenge_from_page(
        self,
        run: FetchSession,
        link: dict[str, Any],
        result: DownloadResult,
    ) -> FetchChallenge | None:
        """Read the portal's answer to a file request as the page it is.

        A document link can answer with a captcha page instead of the file.
        The fetch helper only sees bytes, and the page the portal draws for a
        file request is not always the HTML it returns to ``fetch``, so the
        link is opened the way a person opens it and the resulting page is
        inspected. A page that really is not a captcha is reported as one.
        """

        url = str(link.get("url") or "")
        snapshot = None
        try:
            snapshot = run.driver.open(url)
        except Exception:  # noqa: BLE001 - a failed open leaves the fetched bytes
            snapshot = None

        html = (snapshot.html if snapshot is not None else "") or result.html or ""
        form = parse_captcha_form(html)
        marker = run.driver.find_challenge()
        if form is None and marker is None:
            # Opening the link may have failed; the fetched bytes can still
            # carry the portal's form.
            form = parse_captcha_form(result.html or "")
        if form is None and marker is None:
            return None

        screenshot = run.driver.screenshot() or (
            snapshot.screenshot if snapshot is not None else None
        )
        # The picture of the challenge itself is what a person can answer; the
        # full page is shown beside it for context.
        image = form.image if form is not None else None
        if image is None:
            image = screenshot

        kind = (marker.kind if marker is not None else "captcha") or "captcha"
        if kind in {"blocked", "login"}:
            run.errors.append(
                "The portal stopped the request before the document was handed over."
            )
            return None
        if run.challenges_shown >= _MAX_CHALLENGES_PER_FETCH:
            run.errors.append(
                "The portal asked for more captchas than this fetch will answer. "
                "The remaining documents were not fetched; upload them manually "
                "or start a new fetch."
            )
            run.pending_link = None
            return None
        try:
            run.driver.watch_for_download()
        except Exception:  # noqa: BLE001 - a missing listener is a partial, not a crash
            pass
        run.challenges_shown += 1
        run.pending_link = link
        return FetchChallenge(
            kind="captcha" if kind == "captcha" else kind,
            message=(
                marker.message
                if marker is not None and marker.message
                else "The portal asks for the characters in the picture before it "
                "hands over the document."
            ),
            image=image,
            screenshot=screenshot,
            link=link,
        )

    def _recover_from_page(
        self,
        run: FetchSession,
        link: dict[str, Any],
        snapshot: Any,
    ) -> bool:
        """Take the file from the page an accepted answer produced.

        A correct answer can answer with the file itself, a page with a
        download link, or a "download" page. The original captcha URL is never
        requested again: that request is what draws a new captcha, and asking
        it again is the loop this avoids. Only the new page is read.
        """

        page = getattr(run.driver, "_page", None)
        original = str(link.get("url") or "").strip()
        candidates: list[dict[str, Any]] = []
        if page is not None:
            try:
                candidates = list(PortalWalker._document_links(page))
            except Exception:  # noqa: BLE001 - a page without links is normal
                candidates = []

        current = str(getattr(snapshot, "url", "") or "").strip()
        if current and current != original and _looks_like_file_url(current):
            candidates.insert(
                0,
                {
                    "url": current,
                    "label": str(link.get("label") or "document"),
                    "kind": "unknown",
                },
            )

        seen: set[str] = set()
        ordered: list[dict[str, Any]] = []
        for candidate in _ordered_links(candidates):
            url = str(candidate.get("url") or "").strip()
            if not url or url == original or url in seen:
                continue
            folded = url.casefold()
            if "captcha" in folded or "docdowncaptcha" in folded:
                continue
            seen.add(url)
            ordered.append(candidate)

        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        for candidate in ordered[:_MAX_RECOVERY_LINKS]:
            url = str(candidate.get("url") or "").strip()
            label = str(candidate.get("label") or "").strip() or str(
                link.get("label") or "document"
            )
            result = run.driver.download_in_page(url, max_bytes=max_bytes)
            if not result.ok or result.is_page:
                continue
            added, problems = self._attach_content(
                content=result.content,
                label=label,
                source_url=url,
                max_bytes=max_bytes,
            )
            run.documents.extend(added)
            run.errors.extend(problems)
            if added:
                return True
        return False

    def _consume_pending(self, run: FetchSession) -> FetchChallenge | None:
        """Finish the file an answer produced, or ask for a new answer."""

        link = run.pending_link or {}
        label = str(link.get("label") or "").strip() or "document"
        download = run.driver.take_download()
        if download is None:
            # The job was parked, so nobody was running the page's event loop
            # when the answer was submitted. Pump it once before concluding
            # that no file arrived; otherwise the answer looks lost and the
            # link is fetched again, which asks for a new captcha.
            if run.driver.wait_for_download():
                download = run.driver.take_download()
        if download is not None:
            run.answered_challenges += 1
            run.pending_link = None
            run.rejected_answers = 0
            run.rejected_link = ""
            if download.ok:
                added, problems = self._attach_content(
                    content=download.content,
                    label=label,
                    source_url=str(link.get("url") or ""),
                    max_bytes=self.settings.max_upload_mb * 1024 * 1024,
                )
                run.documents.extend(added)
                run.errors.extend(problems)
            else:
                run.errors.append(
                    f"{label}: {download.error or 'the answered download failed.'}"
                )
            run.index += 1
            return None

        # No file arrived. If the portal is still asking, the answer was not
        # accepted: keep the same request and show the new picture instead of
        # abandoning the document.
        snapshot = None
        try:
            snapshot = run.driver.snapshot()
        except Exception:  # noqa: BLE001
            pass
        html = snapshot.html if snapshot is not None else ""
        text = (getattr(snapshot, "text", "") or "") if snapshot is not None else ""
        folded = text.casefold()
        form = parse_captcha_form(html)
        rejected = form is not None or (
            "invalid captcha" in folded or "answer was not accepted" in folded
        )
        if rejected:
            marker = run.driver.find_challenge()
            key = str(link.get("url") or "")
            if run.rejected_link != key:
                run.rejected_link = key
                run.rejected_answers = 0
            run.rejected_answers += 1
            if run.challenges_shown >= _MAX_CHALLENGES_PER_FETCH:
                run.errors.append(
                    "The portal asked for more captchas than this fetch will "
                    "answer. The remaining documents were not fetched; upload "
                    "them manually or start a new fetch."
                )
                run.pending_link = None
                run.index += 1
                return None
            if run.rejected_answers <= _MAX_REJECTED_ANSWERS:
                run.challenges_shown += 1
                screenshot = run.driver.screenshot() or (
                    snapshot.screenshot if snapshot is not None else None
                )
                image = form.image if form is not None else screenshot
                try:
                    run.driver.watch_for_download()
                except Exception:  # noqa: BLE001
                    pass
                return FetchChallenge(
                    kind="captcha",
                    message=(
                        "That answer was not accepted. The portal has drawn a new "
                        "picture; answer the new one."
                    ),
                    image=image,
                    screenshot=screenshot,
                    link=link,
                )
            run.errors.append(
                f"{label}: the portal did not accept the captcha answer after "
                f"{_MAX_REJECTED_ANSWERS + 1} tries. It was not looped on."
            )
            run.pending_link = None
            run.index += 1
            return None

        # The page is no longer asking for an answer. That is the portal's own
        # next step: read it for the file or its download link. Do not request
        # the original link again, because that request is what draws a new
        # captcha and is exactly the loop this branch avoids.
        run.answered_challenges += 1
        run.pending_link = None
        recovered = False
        if snapshot is not None:
            try:
                recovered = self._recover_from_page(run, link, snapshot)
            except JobCancelled:
                raise
            except Exception:  # noqa: BLE001 - an unread page is reported below
                recovered = False
        if not recovered:
            run.errors.append(
                f"{label}: the portal accepted the answer but did not hand over "
                "the file. The link was not requested again."
            )
        run.index += 1
        return None

    # -- attaching --------------------------------------------------------

    def _attach_content(
        self,
        *,
        content: bytes,
        label: str,
        source_url: str,
        max_bytes: int,
    ) -> tuple[list[str], list[str]]:
        if not content:
            return [], [f"{label}: the download was empty."]
        if len(content) > max_bytes:
            return [], [f"{label}: larger than the download limit."]
        if content[:2] == b"PK":
            return self._attach_bundle(
                content=content, label=label, source_url=source_url, max_bytes=max_bytes
            )
        if content[:4] == b"%PDF":
            document_id, error = self._attach_pdf(
                filename=self._pdf_filename(source_url, label),
                content=content,
                source_url=source_url,
            )
            return ([document_id], []) if document_id else ([], [error or "unknown error"])
        return [], [f"{label}: the downloaded file is not a PDF this product can read."]

    def _attach_bundle(
        self,
        *,
        content: bytes,
        label: str,
        source_url: str,
        max_bytes: int,
    ) -> tuple[list[str], list[str]]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            return [], [f"{label}: the downloaded bundle could not be opened."]

        documents: list[str] = []
        problems: list[str] = []
        total = 0
        for info in archive.infolist()[:60]:
            if info.is_dir():
                continue
            name = info.filename
            if not name.lower().endswith(".pdf"):
                problems.append(f"{name}: not a PDF, so it was not attached.")
                continue
            if info.file_size > max_bytes:
                problems.append(f"{name}: larger than the upload limit.")
                continue
            total += info.file_size
            if total > max_bytes * 3:
                problems.append("The bundle holds more than the upload limit.")
                break
            try:
                data = archive.read(info)
            except Exception:  # noqa: BLE001
                problems.append(f"{name}: could not be read from the bundle.")
                continue
            document_id, error = self._attach_pdf(
                filename=Path(name).name,
                content=data,
                source_url=source_url,
            )
            if document_id:
                documents.append(document_id)
            elif error:
                problems.append(error)
        return documents, problems

    def _attach_pdf(
        self,
        *,
        filename: str,
        content: bytes,
        source_url: str,
    ) -> tuple[str | None, str | None]:
        try:
            document = self.documents.upload(
                tender_id=self._tender_id,
                filename=filename,
                data=content,
                source_url=source_url,
                company_id=self.company_id,
                actor_user_id=self.actor_user_id,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            return None, f"{filename}: {exc}"
        return str(document.id), None

    @property
    def _tender_id(self) -> uuid.UUID:
        value = getattr(self, "_current_tender_id", None)
        if value is None:
            raise RuntimeError("No tender is being fetched.")
        return value

    @staticmethod
    def _pdf_filename(url: str, label: str) -> str:
        path = urlsplit(url).path or ""
        name = path.rsplit("/", 1)[-1]
        if not name.lower().endswith(".pdf"):
            stem = label.rsplit(".", 1)[0] or "tender-document"
            name = f"{stem}.pdf"
        return name[:200]

    # -- covers and state -------------------------------------------------

    def _store_covers(self, tender_id: uuid.UUID, covers: list[dict[str, Any]]) -> None:
        if not covers:
            return
        existing = self.aux.covers(tender_id)
        current = [
            (row.cover_no, row.doc_type, row.description) for row in existing
        ]
        incoming = [
            (row["cover_no"], row.get("doc_type"), row.get("description"))
            for row in covers
        ]
        if current == incoming:
            return
        self.aux.replace_covers(tender_id, "v1", covers)
        self.session.commit()

    def _metrics(self, run: FetchSession) -> dict[str, Any]:
        tender = self.tenders.get(run.tender_id)
        covers = self.aux.covers(run.tender_id) if tender is not None else []
        documents = (
            self.documents.list_for_tender(run.tender_id, include_non_current=False)
            if tender is not None
            else []
        )
        missing = missing_covers(covers, documents)
        return {
            "documents": len(documents),
            "covers_expected": len(covers) if covers else None,
            "covers_missing": missing,
            "challenges_answered": run.answered_challenges,
            "errors": len(run.errors),
        }

    def _done_note(self, run: FetchSession) -> str | None:
        covers = self.aux.covers(run.tender_id)
        documents = self.documents.list_for_tender(
            run.tender_id, include_non_current=False
        )
        missing = missing_covers(covers, documents)
        if missing:
            return (
                f"Attached {len(documents)} document(s); cover(s) "
                f"{', '.join(str(number) for number in missing)} not yet fetched."
            )
        if run.errors:
            return (
                f"Attached {len(documents)} document(s); "
                f"{len(run.errors)} link(s) could not be attached."
            )
        if documents:
            return f"Attached {len(documents)} document(s)."
        return "No document could be fetched."

    def _finish(
        self,
        run: FetchSession,
        outcome: FetchOutcome,
        *,
        challenge_pending: bool = False,
    ) -> FetchOutcome:
        outcome.metrics = {**self._metrics(run), **outcome.metrics}
        tender = self.tenders.get(run.tender_id)
        if tender is None:
            return outcome
        state = self.aux.ensure_fetch_state(
            run.tender_id,
            documents_present=int(outcome.metrics.get("documents") or 0),
        )
        state.last_attempt_at = utcnow()
        state.attempts += 1
        covers = self.aux.covers(run.tender_id)
        state.expected_covers = len(covers) if covers else None
        state.missing_covers = list(outcome.metrics.get("covers_missing") or [])
        if getattr(run.driver, "lost", False):
            # A closed window is the whole story; a per-file error underneath
            # it would read as a document problem that can be retried.
            outcome.state = "partial"
            outcome.notes = SESSION_LOST_NOTE
            state.last_error = SESSION_LOST_NOTE
        elif challenge_pending:
            state.last_error = "Waiting for the human to answer the captcha."
        else:
            state.last_error = "; ".join(outcome.errors)[:2000] or None
        state.updated_at = utcnow()
        self.session.commit()
        return outcome


def enqueue_fetch(
    session: Session,
    *,
    tender_id: uuid.UUID,
    company_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    settings: Settings | None = None,
) -> tuple[Any, bool]:
    """Queue the one global fetch for a tender, or return the live one."""

    settings = settings or get_settings()
    if not settings.discovery_enabled:
        from app.core.errors import ServiceUnavailable

        raise ServiceUnavailable("Document fetching is not enabled in this deployment.")
    tenders = TenderRepository(session)
    tender = tenders.get(tender_id)
    if tender is None:
        from app.core.errors import NotFound

        raise NotFound("Tender not found.")
    locator = tenders.locator(tender_id)
    if locator is None and tender.source_type == "manual_upload":
        raise BadRequest(
            "This is a manually uploaded tender. Upload its documents on the "
            "Documents tab; there is no portal page to fetch."
        )
    if locator is None and not tender.source_url:
        raise BadRequest(
            "This tender has no portal locator or source address, so its "
            "documents cannot be fetched. Upload them manually."
        )

    existing = JobRepository(session).active_for_payload(
        "fetch_documents", "tender_id", str(tender_id)
    )
    if existing is not None:
        return existing, False

    aux = TenderAuxRepository(session)
    count = DocumentRepository(session).count_current(tender_id)
    state = aux.ensure_fetch_state(tender_id, documents_present=count)
    state.last_requested_at = utcnow()
    state.updated_at = utcnow()
    session.flush()

    job, created = JobService(session, settings).enqueue(
        kind="fetch_documents",
        payload={
            "tender_id": str(tender_id),
            "company_id": str(company_id) if company_id else None,
            "user_id": str(user_id) if user_id else None,
        },
        company_id=None,
        lane="browser",
        priority=40,
    )
    return job, created
