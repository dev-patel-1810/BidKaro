"""Cited requirement extraction and human review.

Deterministic code owns chunking, schema validation, the citation gate and
persistence. The AI provider only proposes requirements; an excerpt is stored
as `verified` only when it is found on the cited page of the same document and
that page has a text layer. OCR-only matches start as `needs_review`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict, NotFound, ServiceUnavailable
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.integrations.ai import AIError, AIProvider
from app.models.corpus import DocumentPage, Requirement, TenderDocument
from app.repositories.corpus import (
    DocumentRepository,
    RequirementFlagRepository,
    RequirementRepository,
    TenderRepository,
)
from app.repositories.identity import AuditRepository
from app.services.corpus_rules import (
    MAX_EXCERPT_CHARS,
    MAX_OBLIGATION_CHARS,
    MAX_REFERENCE_CHARS,
    MAX_TITLE_CHARS,
    excerpt_hash,
    is_navigation_excerpt,
    map_category,
    map_mandatory,
    map_phase,
    normalize_for_match,
    parse_deadline_at,
    trim,
)

PROMPT_VERSION = "requirements-v2"
MIN_EXCERPT_CHARS = 20
MAX_PAGE_TEXT_CHARS = 6000
REVIEW_STATES = {"needs_review", "rejected", "accepted_ocr"}
STATE_VALUES = {"verified", "needs_review", "accepted_ocr", "unverified", "rejected"}


SYSTEM_PROMPT = """You extract requirements from public tender documents for a bid-compliance tool.

Rules:
1. Return JSON only, shaped as {"requirements": [...]}.
2. Every requirement must include "title", "obligation", "excerpt", "page_index",
   "category", "phase" and "mandatory_state". "excerpt" must be copied
   character-for-character from the page text. Never paraphrase, summarise,
   correct or invent the excerpt. If you cannot quote it, omit the requirement.
3. "page_index" must be the page number from the [[PAGE n]] marker that contains
   the excerpt.
4. "obligation" is your plain-language interpretation of the excerpt. Keep facts
   (dates, amounts, percentages) exactly as written and do not add requirements
   the text does not state.
5. "category" must be one of: eligibility, submission, compliance, form, performance.
6. "phase" must be "pre_award" or "post_award". Use "post_award" only for an
   obligation that starts after award (for example performance security,
   liquidated damages or warranty).
7. "mandatory_state" must be "mandatory", "not_mandatory" or "unknown". Use
   "unknown" when the text does not make it explicit.
8. "clause_ref" and "form_ref" are optional. Set "clause_ref" whenever the text
   numbers the clause. Set "form_ref" only when a form is named.
9. "deadline_text", "deadline_at", "consequence_text", "acceptance_criteria",
   "issuer", "signatory" and "evidence_check" are optional. Copy dates and
   consequences from the text; never invent them. "deadline_at" is an ISO date
   only when the document states one.
10. Do not output boilerplate advice, general legal suggestions, or requirements
    that are not in the text.

Example item:
{"category": "eligibility", "phase": "pre_award",
 "title": "Minimum average annual turnover",
 "obligation": "The bidder must show average annual turnover of at least INR 5 crore.",
 "excerpt": "The bidder shall have an average annual turnover of at least INR 5 crore.",
 "page_index": 3, "clause_ref": "3.1", "form_ref": null,
 "mandatory_state": "mandatory", "deadline_text": null, "deadline_at": null,
 "consequence_text": null, "acceptance_criteria": null, "issuer": null,
 "signatory": null, "evidence_check": null}

Return {"requirements": []} when the pages contain no extractable requirements."""


class AIRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: str = "compliance"
    phase: str = "pre_award"
    title: str | None = None
    obligation: str | None = None
    excerpt: str
    page_index: int = Field(ge=1, le=10000)
    clause_ref: str | None = None
    clause_reference: str | None = None
    form_ref: str | None = None
    form_reference: str | None = None
    mandatory_state: str | None = None
    mandatory: str | None = None
    deadline_text: str | None = None
    deadline_at: str | None = None
    consequence_text: str | None = None
    acceptance_criteria: str | None = None
    issuer: str | None = None
    signatory: str | None = None
    evidence_check: str | None = None


def parse_ai_items(payload: Any) -> tuple[list[AIRequirement], int]:
    """Validate items individually so one malformed item cannot sink a chunk."""
    if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
        raise AIError("AI response did not contain a requirements list.")
    items: list[AIRequirement] = []
    invalid = 0
    for raw in payload["requirements"]:
        try:
            items.append(AIRequirement.model_validate(raw))
        except PydanticValidationError:
            invalid += 1
    return items, invalid


@dataclass(slots=True)
class PageChunk:
    pages: list[dict]


@dataclass(slots=True)
class ExtractionStats:
    created: int = 0
    duplicates: int = 0
    invalid: int = 0
    verified: int = 0
    unverified: int = 0
    needs_review: int = 0
    failed_chunks: int = 0
    last_error: str | None = None


@dataclass(slots=True)
class RequirementExtractionResult:
    document_id: uuid.UUID
    status: str
    chunks: int
    skipped_chunks: int
    stats: ExtractionStats
    model: str
    message: str
    restored_previous: bool = False

    def metrics(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "status": self.status,
            "chunks": self.chunks,
            "skipped_chunks": self.skipped_chunks,
            "created": self.stats.created,
            "duplicates": self.stats.duplicates,
            "invalid": self.stats.invalid,
            "verified": self.stats.verified,
            "unverified": self.stats.unverified,
            "needs_review": self.stats.needs_review,
            "failed_chunks": self.stats.failed_chunks,
            "model": self.model,
            "restored_previous": self.restored_previous,
        }


def chunk_pages(
    pages: Iterable[dict],
    *,
    max_chars: int = MAX_PAGE_TEXT_CHARS,
    max_pages_per_chunk: int = 3,
) -> list[PageChunk]:
    chunks: list[PageChunk] = []
    current: list[dict] = []
    current_chars = 0

    for page in pages:
        if not page.get("has_text"):
            continue
        size = len(page.get("text") or "")
        if current and (
            current_chars + size > max_chars
            or len(current) >= max(max_pages_per_chunk, 1)
        ):
            chunks.append(PageChunk(pages=current))
            current = []
            current_chars = 0
        current.append(page)
        current_chars += size

    if current:
        chunks.append(PageChunk(pages=current))
    return chunks


def _has_text(text: str | None) -> bool:
    return len(" ".join((text or "").split())) >= 40


def _page_dict(page: DocumentPage) -> dict:
    return {
        "page_index": page.page_index,
        "printed_label": page.printed_label,
        "text": page.text or "",
        "has_text": _has_text(page.text),
        "is_ocr": page.is_ocr,
    }


class RequirementService:
    def __init__(
        self,
        session: Session,
        *,
        ai: AIProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.ai = ai
        self.tenders = TenderRepository(session)
        self.documents = DocumentRepository(session)
        self.requirements = RequirementRepository(session)
        self.flags = RequirementFlagRepository(session)
        self.audit = AuditRepository(session)

    # -- extraction -------------------------------------------------------

    def extract(
        self,
        *,
        document_id: uuid.UUID,
        replace: bool = False,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        progress: Callable[[], None] | None = None,
    ) -> RequirementExtractionResult:
        document = self.documents.get(document_id)
        if document is None:
            raise NotFound("Tender document not found.")
        tender = self.tenders.get(document.tender_id)
        if tender is None:
            raise NotFound("Tender not found.")

        pages = [_page_dict(page) for page in self.documents.pages(document_id)]
        text_pages = [page for page in pages if page["has_text"]]
        if not text_pages:
            raise BadRequest(
                "This PDF has no extractable text. OCR did not produce usable "
                "text; upload a clearer digital copy or enable OCR in the "
                "server settings."
            )
        if self.ai is None or not self.ai.configured:
            raise ServiceUnavailable(
                "AI extraction is not configured. Set AI_PROVIDER, the provider "
                "API key and model."
            )

        backup_ids: list[uuid.UUID] = []
        if replace:
            backup_ids = self.requirements.deactivate_ai(document_id)

        selected = text_pages[: max(self.settings.ai_extraction_max_pages, 1)]
        all_chunks = chunk_pages(
            selected,
            max_chars=self.settings.ai_extraction_max_chunk_chars,
            max_pages_per_chunk=self.settings.ai_extraction_max_pages_per_chunk,
        )
        chunks = all_chunks[: max(self.settings.ai_extraction_max_chunks, 1)]
        skipped_chunks = len(all_chunks) - len(chunks)

        stats = ExtractionStats()
        pages_by_index = {page["page_index"]: page for page in pages}

        try:
            for chunk in chunks:
                if progress is not None:
                    progress()
                try:
                    payload = self.ai.complete_json(
                        system=SYSTEM_PROMPT,
                        user=self._chunk_prompt(tender, document, chunk),
                    )
                    items, invalid = parse_ai_items(payload)
                    stats.invalid += invalid
                except AIError as exc:
                    stats.failed_chunks += 1
                    stats.last_error = str(exc)
                    continue

                for item in items:
                    self._store_requirement(
                        item=item,
                        document=document,
                        pages_by_index=pages_by_index,
                        stats=stats,
                    )

            restored = False
            if replace and backup_ids and stats.created == 0:
                self.requirements.reactivate(backup_ids)
                restored = True

            status = "completed" if stats.failed_chunks == 0 else "partial"
            message = (
                f"Extracted {stats.created} requirements "
                f"({stats.verified} verified, {stats.unverified} unverified)."
            )
            if stats.needs_review:
                message += (
                    f" {stats.needs_review} matched OCR text and need visual review."
                )
            if stats.duplicates:
                message += f" Skipped {stats.duplicates} duplicates."
            if stats.invalid:
                message += f" Ignored {stats.invalid} malformed items."
            if skipped_chunks:
                message += f" {skipped_chunks} chunks were skipped by the per-run limit."
            if stats.failed_chunks:
                message += (
                    f" {stats.failed_chunks} chunks failed; last error: "
                    f"{stats.last_error or 'unknown'}."
                )
            if restored:
                message += " Existing requirements were kept because none were replaced."
            if not chunks:
                status = "no_text"
                message = "No extractable pages were available for AI extraction."

            self.audit.log(
                action="requirements.extracted",
                entity_type="tender_document",
                company_id=company_id,
                actor_user_id=actor_user_id,
                entity_id=document_id,
                after={
                    "tender_id": str(document.tender_id),
                    "model": self.ai.model,
                    "chunks": len(chunks),
                    "created": stats.created,
                    "unverified": stats.unverified,
                    "restored_previous": restored,
                },
            )
            result = RequirementExtractionResult(
                document_id=document_id,
                status=status,
                chunks=len(chunks),
                skipped_chunks=skipped_chunks,
                stats=stats,
                model=self.ai.model,
                message=message,
                restored_previous=restored,
            )
            self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise

    def _chunk_prompt(
        self, tender, document: TenderDocument, chunk: PageChunk
    ) -> str:
        parts = [
            f"Tender title: {tender.work_title or 'not stated'}",
            f"Authority: {tender.authority or 'not stated'}",
            f"Document: {document.filename}",
            "",
            "Extract requirements only from the pages below. Use the exact page "
            "number from each marker.",
            "",
        ]
        for page in chunk.pages:
            parts.append(
                f"[[PAGE {page['page_index']} | PRINTED "
                f"{page.get('printed_label') or page['page_index']}]]"
            )
            parts.append(page.get("text") or "")
            parts.append("")
        return "\n".join(parts)

    def _validate_excerpt(
        self, item: AIRequirement, page: dict | None
    ) -> str | None:
        excerpt = (item.excerpt or "").strip()
        if len(excerpt) < MIN_EXCERPT_CHARS:
            return "Excerpt is too short to verify."
        if len(excerpt) > MAX_EXCERPT_CHARS:
            return "Excerpt is too long to verify."
        if page is None:
            return "The cited page does not exist in this document."
        if not page.get("has_text"):
            return "The cited page has no extractable text."
        if normalize_for_match(excerpt) not in normalize_for_match(page.get("text") or ""):
            return "The excerpt was not found verbatim on the cited page."
        return None

    def _store_requirement(
        self,
        *,
        item: AIRequirement,
        document: TenderDocument,
        pages_by_index: dict[int, dict],
        stats: ExtractionStats,
    ) -> None:
        excerpt = (item.excerpt or "").strip()
        if len(excerpt) < MIN_EXCERPT_CHARS:
            stats.invalid += 1
            return
        page_index = int(item.page_index)
        digest = excerpt_hash(excerpt)
        if self.requirements.exists(document.id, page_index, digest):
            stats.duplicates += 1
            return

        page = pages_by_index.get(page_index)
        reason = self._validate_excerpt(item, page)
        is_ocr = bool(page and page.get("is_ocr"))
        ocr_review = False
        if reason is None and is_ocr:
            reason = "Excerpt matched OCR text only; verify it visually against the page image."
            ocr_review = True

        title = trim(item.title, MAX_TITLE_CHARS)
        clause = trim(item.clause_ref or item.clause_reference, MAX_REFERENCE_CHARS)
        if not title:
            title = (
                f"Clause {clause}"
                if clause
                else (trim(item.obligation, 100) or excerpt[:100])
            )

        mandatory = item.mandatory_state or item.mandatory
        deadline_raw = item.deadline_at
        if not deadline_raw and item.deadline_text:
            candidate = item.deadline_text.strip()
            if len(candidate) >= 10 and candidate[:4].isdigit():
                deadline_raw = candidate[:10]

        values = {
            "document_id": document.id,
            "page_index": page_index,
            "printed_label": page.get("printed_label") if page else None,
            "clause_ref": clause,
            "form_ref": trim(item.form_ref or item.form_reference, MAX_REFERENCE_CHARS),
            "category": map_category(item.category),
            "phase": map_phase(item.phase),
            "title": title,
            "obligation": trim(item.obligation, MAX_OBLIGATION_CHARS)
            or excerpt[:MAX_OBLIGATION_CHARS],
            "deadline_text": trim(item.deadline_text, 500),
            "deadline_at": parse_deadline_at(deadline_raw),
            "consequence_text": trim(item.consequence_text, 1000),
            "acceptance_criteria": trim(item.acceptance_criteria, 1000),
            "issuer": trim(item.issuer, 200),
            "signatory": trim(item.signatory, 200),
            "evidence_check": trim(item.evidence_check, 500),
            "excerpt": excerpt[:MAX_EXCERPT_CHARS],
            "excerpt_normalised": normalize_for_match(excerpt),
            "excerpt_hash": digest,
            "is_navigation": is_navigation_excerpt(excerpt, item.obligation),
            "state": "needs_review"
            if ocr_review
            else ("unverified" if reason else "verified"),
            "state_reason": reason,
            "mandatory_state": map_mandatory(mandatory),
            "origin": "ai",
            "model": self.ai.model if self.ai is not None else None,
            "prompt_version": PROMPT_VERSION,
            "is_active": True,
        }

        try:
            with self.session.begin_nested():
                self.requirements.create(values)
        except IntegrityError:
            stats.duplicates += 1
            return

        stats.created += 1
        if ocr_review:
            stats.needs_review += 1
        elif reason:
            stats.unverified += 1
        else:
            stats.verified += 1

    # -- reads ------------------------------------------------------------

    def list_for_tender(
        self,
        tender_id: uuid.UUID,
        *,
        company_id: uuid.UUID | None = None,
        state: list[str] | None = None,
        category: str | None = None,
        include_navigation: bool = False,
        include_inactive: bool = False,
        include_ignored: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        if self.tenders.get(tender_id) is None:
            raise NotFound("Tender not found.")
        rows, total = self.requirements.list_for_tender(
            tender_id,
            state=state,
            category=category,
            include_navigation=include_navigation,
            include_inactive=include_inactive,
            company_id=company_id,
            include_ignored=include_ignored,
            limit=limit,
            offset=offset,
        )
        flags = (
            {
                flag.requirement_id: flag
                for flag in (
                    self.flags.get(company_id, row.id) for row in rows
                )
                if flag is not None
            }
            if company_id is not None
            else {}
        )
        return [self.out(row, flag=flags.get(row.id)) for row in rows], total

    def get(self, requirement_id: uuid.UUID) -> Requirement:
        requirement = self.requirements.get(requirement_id)
        if requirement is None:
            raise NotFound("Requirement not found.")
        return requirement

    def get_out(
        self, requirement_id: uuid.UUID, *, company_id: uuid.UUID | None = None
    ) -> dict:
        requirement = self.get(requirement_id)
        flag = (
            self.flags.get(company_id, requirement.id)
            if company_id is not None
            else None
        )
        return self.out(requirement, flag=flag)

    def out(self, requirement: Requirement, *, flag=None) -> dict[str, Any]:
        document = requirement.document
        return {
            "id": requirement.id,
            "document_id": requirement.document_id,
            "tender_id": document.tender_id if document is not None else None,
            "page_index": requirement.page_index,
            "printed_label": requirement.printed_label,
            "clause_ref": requirement.clause_ref,
            "form_ref": requirement.form_ref,
            "category": requirement.category,
            "phase": requirement.phase,
            "title": requirement.title,
            "obligation": requirement.obligation,
            "deadline_text": requirement.deadline_text,
            "deadline_at": requirement.deadline_at,
            "consequence_text": requirement.consequence_text,
            "acceptance_criteria": requirement.acceptance_criteria,
            "issuer": requirement.issuer,
            "signatory": requirement.signatory,
            "evidence_check": requirement.evidence_check,
            "excerpt": requirement.excerpt,
            "is_navigation": requirement.is_navigation,
            "state": requirement.state,
            "state_reason": requirement.state_reason,
            "mandatory_state": requirement.mandatory_state,
            "origin": requirement.origin,
            "model": requirement.model,
            "prompt_version": requirement.prompt_version,
            "is_active": requirement.is_active,
            "reviewed_by": requirement.reviewed_by,
            "reviewed_at": requirement.reviewed_at,
            "created_at": requirement.created_at,
            "updated_at": requirement.updated_at,
            "document": {
                "id": document.id,
                "filename": document.filename,
                "version": document.version,
                "is_current": document.is_current,
                "has_text_layer": document.has_text_layer,
                "source_url": document.source_url,
                "fetched_at": document.fetched_at,
            }
            if document is not None
            else None,
            "flagged": bool(flag.ignored) if flag is not None else False,
            "flag_note": flag.note if flag is not None else None,
        }

    def summary(self, tender_id: uuid.UUID) -> dict[str, int]:
        if self.tenders.get(tender_id) is None:
            raise NotFound("Tender not found.")
        return self.requirements.summary(tender_id)

    # -- human actions ----------------------------------------------------

    def review(
        self,
        *,
        requirement_id: uuid.UUID,
        state: str,
        note: str | None,
        company_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
    ) -> dict:
        if state not in REVIEW_STATES:
            raise BadRequest(
                "A human review may set needs_review, rejected or accepted_ocr. "
                "Only the citation gate can set verified."
            )
        requirement = self.get(requirement_id)
        if state == "accepted_ocr":
            page = self.documents.page(requirement.document_id, requirement.page_index)
            if page is None or not page.is_ocr:
                raise BadRequest(
                    "Only a citation from an OCR page can be accepted as an OCR "
                    "citation. This page has a text layer."
                )

        before = requirement.state
        requirement.state = state
        if note is not None:
            requirement.state_reason = trim(note, 1000)
        elif state in {"accepted_ocr", "needs_review", "rejected"}:
            # Keep the original machine reason for an unverified citation; a
            # human note is optional and never overwrites evidence silently.
            pass
        requirement.reviewed_by = actor_user_id
        requirement.reviewed_at = utcnow()
        requirement.updated_at = utcnow()
        self.audit.log(
            action="requirement.reviewed",
            entity_type="requirement",
            company_id=company_id,
            actor_user_id=actor_user_id,
            entity_id=requirement.id,
            before={"state": before},
            after={"state": state, "note": note},
        )
        self.session.commit()
        return self.out(
            requirement,
            flag=self.flags.get(company_id, requirement.id)
            if company_id is not None
            else None,
        )

    def create_human(
        self,
        *,
        tender_id: uuid.UUID,
        document_id: uuid.UUID,
        page_index: int,
        excerpt: str,
        category: str,
        title: str | None,
        obligation: str | None,
        mandatory_state: str = "unknown",
        phase: str = "pre_award",
        clause_ref: str | None = None,
        form_ref: str | None = None,
        deadline_text: str | None = None,
        consequence_text: str | None = None,
        acceptance_criteria: str | None = None,
        issuer: str | None = None,
        signatory: str | None = None,
        evidence_check: str | None = None,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> dict:
        if self.tenders.get(tender_id) is None:
            raise NotFound("Tender not found.")
        document = self.documents.get(document_id)
        if document is None or document.tender_id != tender_id:
            raise NotFound("Tender document not found.")
        page = self.documents.page(document_id, page_index)
        if page is None:
            raise BadRequest("The cited page does not exist in this document.")

        cleaned = (excerpt or "").strip()
        if len(cleaned) < MIN_EXCERPT_CHARS:
            raise BadRequest("The excerpt is too short to cite a page.")
        digest = excerpt_hash(cleaned)
        if self.requirements.exists(document_id, page_index, digest):
            raise Conflict("That excerpt is already cited on this page.")

        normalised_page = normalize_for_match(page.text)
        found = normalize_for_match(cleaned) in normalised_page
        if found and page.is_ocr:
            state = "needs_review"
            reason = "Human citation matched OCR text only; verify it visually."
        elif found:
            state = "verified"
            reason = None
        else:
            state = "unverified"
            reason = "The excerpt was not found verbatim on the cited page."

        values = {
            "document_id": document_id,
            "page_index": page_index,
            "printed_label": page.printed_label,
            "clause_ref": trim(clause_ref, MAX_REFERENCE_CHARS),
            "form_ref": trim(form_ref, MAX_REFERENCE_CHARS),
            "category": map_category(category),
            "phase": map_phase(phase),
            "title": trim(title, MAX_TITLE_CHARS) or cleaned[:100],
            "obligation": trim(obligation, MAX_OBLIGATION_CHARS)
            or cleaned[:MAX_OBLIGATION_CHARS],
            "deadline_text": trim(deadline_text, 500),
            "deadline_at": parse_deadline_at(deadline_text),
            "consequence_text": trim(consequence_text, 1000),
            "acceptance_criteria": trim(acceptance_criteria, 1000),
            "issuer": trim(issuer, 200),
            "signatory": trim(signatory, 200),
            "evidence_check": trim(evidence_check, 500),
            "excerpt": cleaned[:MAX_EXCERPT_CHARS],
            "excerpt_normalised": normalize_for_match(cleaned),
            "excerpt_hash": digest,
            "is_navigation": is_navigation_excerpt(cleaned, obligation),
            "state": state,
            "state_reason": reason,
            "mandatory_state": map_mandatory(mandatory_state),
            "origin": "human",
            "model": None,
            "prompt_version": None,
            "is_active": True,
            "reviewed_by": actor_user_id,
            "reviewed_at": utcnow(),
        }
        requirement = self.requirements.create(values)
        self.audit.log(
            action="requirement.created",
            entity_type="requirement",
            company_id=company_id,
            actor_user_id=actor_user_id,
            entity_id=requirement.id,
            after={
                "tender_id": str(tender_id),
                "document_id": str(document_id),
                "state": state,
                "page_index": page_index,
            },
        )
        self.session.commit()
        return self.out(requirement)

    def flag(
        self,
        *,
        requirement_id: uuid.UUID,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        ignored: bool | None = None,
        note: str | None = None,
        set_note: bool = False,
    ) -> dict:
        requirement = self.get(requirement_id)
        row = self.flags.upsert(
            company_id=company_id,
            requirement_id=requirement.id,
            ignored=ignored,
            note=trim(note, 1000),
            set_note=set_note,
        )
        self.audit.log(
            action="requirement.flagged",
            entity_type="requirement",
            company_id=company_id,
            actor_user_id=actor_user_id,
            entity_id=requirement.id,
            after={"ignored": row.ignored, "note": row.note},
        )
        self.session.commit()
        return self.out(requirement, flag=row)
