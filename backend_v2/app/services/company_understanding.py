"""Read an about-company profile into cited capabilities and experiences.

The deterministic term harvest (``company_intel``) answers "which words does
this company use". This pass answers the two questions discovery actually
needs:

* what *kinds of work* does the company do, and
* what has it already delivered (projects, products, clients)?

The model may only quote. Every item it returns is checked against the page
text before it is stored: the quote has to be found on the page it names, or
the item is dropped. An item is stored ``proposed`` and is shown to a human;
only a human-confirmed capability is ever handed to the similarity judge, and
only a human-confirmed experience counts as "we have done this before".

This is never eligibility. A capability cannot make a mandatory criterion a
pass, and a missing experience is "new work to look at", not a fail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, NotFound
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.integrations.ai import AIError, AIProvider
from app.models.identity import Company, CompanyDocument
from app.repositories.understanding import CompanyUnderstandingRepository
from app.services.corpus_rules import normalize_for_match, trim
from app.services.requirement_service import PageChunk, chunk_pages

#: The reading rules, not the matching rules. A stored item carries this stamp
#: so a changed reader can be recognised later.
UNDERSTANDING_VERSION = "2026-11-18.1"

MAX_CAPABILITIES = 30
MAX_EXPERIENCES = 30
MAX_ALIASES = 4
MIN_QUOTE_CHARS = 12
MAX_EXCERPT_CHARS = 500
MAX_DESCRIPTION_CHARS = 500

SYSTEM_PROMPT = """You read one company's own profile document and report the work it does.
Return JSON only, in exactly this shape:
{"capabilities": [{"name": "...", "description": "...", "aliases": ["..."],
  "page": 3, "quote": "..."}],
 "experiences": [{"title": "...", "client": "...", "description": "...",
  "work_family": "...", "page": 4, "quote": "..."}]}

Rules, all mandatory:
- Quote word for word from the page. Never paraphrase a quote, never merge two
  sentences, and never write a quote that is not on the page.
- "page" is the page number printed in the input.
- A capability is a kind of work the company itself does, in a short noun
  phrase ("document digitisation", "road maintenance"). It is not a client, an
  authority, a location, a certification, a company name or the word
  "services".
- "description" is one plain sentence in your own words (at most 40 words)
  explaining the capability, with an example if the page gives one.
- "aliases" are 0 to 4 alternative wordings of the same work that a tender
  might use.
- An experience is a named project, contract or product the company delivered,
  with its client when the page names one. "work_family" says which capability
  it evidences, using the capability's name.
- Report only what the page states. If the page does not state it, leave it
  out. Empty lists are a valid answer.
- You are not deciding eligibility, value, deadlines or whether the company
  should bid. Only what kind of work this is.
"""


class CapabilityAnswer(BaseModel):
    name: str = Field(default="", max_length=200)
    description: str | None = Field(default=None, max_length=800)
    aliases: list[str] = Field(default_factory=list)
    page: int | None = None
    quote: str = Field(default="", max_length=1200)


class ExperienceAnswer(BaseModel):
    title: str = Field(default="", max_length=300)
    client: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=800)
    work_family: str | None = Field(default=None, max_length=200)
    page: int | None = None
    quote: str = Field(default="", max_length=1200)


class UnderstandingAnswer(BaseModel):
    capabilities: list[CapabilityAnswer] = Field(default_factory=list)
    experiences: list[ExperienceAnswer] = Field(default_factory=list)


@dataclass(slots=True)
class UnderstandingResult:
    state: str
    error: str | None
    capabilities: int = 0
    experiences: int = 0
    chunks: int = 0
    failed_chunks: int = 0
    dropped: int = 0
    model: str | None = None


@dataclass(slots=True)
class _Pages:
    """The page text, keyed by 1-based page number, for quote checks."""

    by_number: dict[int, dict] = field(default_factory=dict)

    def locate(self, quote: str, page_number: int | None) -> dict | None:
        target = normalize_for_match(quote)
        if not target:
            return None
        if page_number is not None:
            page = self.by_number.get(int(page_number))
            if page is not None and target in normalize_for_match(page["text"]):
                return page
        for page in self.by_number.values():
            if target in normalize_for_match(page["text"]):
                return page
        return None


def _clean(value: str | None, limit: int) -> str | None:
    return trim(value, limit)


def _aliases(values: list[str], name: str) -> list[str]:
    seen: list[str] = []
    key = normalize_for_match(name)
    for value in values or []:
        cleaned = _clean(value, 80)
        if not cleaned or normalize_for_match(cleaned) == key:
            continue
        if normalize_for_match(cleaned) in {normalize_for_match(item) for item in seen}:
            continue
        seen.append(cleaned)
        if len(seen) >= MAX_ALIASES:
            break
    return seen


def _valid_capability(
    item: CapabilityAnswer,
    *,
    pages: _Pages,
    company_name: str | None,
    model: str | None,
) -> dict[str, Any] | None:
    name = _clean(item.name, 160)
    quote = _clean(item.quote, 1000)
    if not name or len(name) < 2 or not quote or len(quote) < MIN_QUOTE_CHARS:
        return None
    if company_name and normalize_for_match(name) == normalize_for_match(company_name):
        return None
    page = pages.locate(quote, item.page)
    if page is None:
        return None
    return {
        "name": name,
        "normalized": normalize_for_match(name)[:160],
        "description": _clean(item.description, MAX_DESCRIPTION_CHARS),
        "aliases": _aliases(item.aliases, name),
        "page_index": page["page_index"],
        "printed_label": page.get("printed_label"),
        "excerpt": quote[:MAX_EXCERPT_CHARS],
        "quote_verified": True,
        "is_ocr": bool(page.get("is_ocr")),
        "origin": "ai",
        "model": model,
        "prompt_version": UNDERSTANDING_VERSION,
    }


def _valid_experience(
    item: ExperienceAnswer,
    *,
    pages: _Pages,
    model: str | None,
) -> dict[str, Any] | None:
    title = _clean(item.title, 200)
    quote = _clean(item.quote, 1000)
    if not title or len(title) < 2 or not quote or len(quote) < MIN_QUOTE_CHARS:
        return None
    page = pages.locate(quote, item.page)
    if page is None:
        return None
    return {
        "title": title,
        "normalized": normalize_for_match(title)[:200],
        "client": _clean(item.client, 200),
        "description": _clean(item.description, MAX_DESCRIPTION_CHARS),
        "work_family": _clean(item.work_family, 200),
        "value_amount": None,
        "currency": "INR",
        "completed_on": None,
        "page_index": page["page_index"],
        "printed_label": page.get("printed_label"),
        "excerpt": quote[:MAX_EXCERPT_CHARS],
        "quote_verified": True,
        "is_ocr": bool(page.get("is_ocr")),
        "origin": "ai",
        "model": model,
        "prompt_version": UNDERSTANDING_VERSION,
    }


def _chunk_prompt(chunk: PageChunk) -> str:
    parts: list[str] = ["Company profile pages:"]
    for page in chunk.pages:
        # Page indexes are 1-based throughout the corpus (a requirement's
        # ``page_index`` is the page a reader sees), so the number shown to the
        # model is the stored one.
        number = int(page["page_index"])
        label = page.get("printed_label")
        heading = f"### Page {number}"
        if label and str(label) != str(number):
            heading += f" (printed label: {label})"
        parts.append(f"{heading}\n{page['text']}")
    return "\n\n".join(parts)


class CompanyUnderstandingService:
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
        self.repository = CompanyUnderstandingRepository(session)

    # ------------------------------------------------------------------ read
    def understanding(self, company: Company) -> dict[str, Any]:
        documents = self.repository.understanding_documents(company.id)
        capabilities = self.repository.capabilities_for_company(company.id)
        experiences = self.repository.experiences_for_company(company.id)
        return {
            "documents": [
                {
                    "document_id": document.id,
                    "title": document.title,
                    "understanding_state": document.understanding_state,
                    "understanding_error": document.understanding_error,
                    "understanding_at": document.understanding_at,
                    "capability_count": document.capability_count,
                    "experience_count": document.experience_count,
                }
                for document in documents
            ],
            "capabilities": [self.capability_out(row) for row in capabilities],
            "experiences": [self.experience_out(row) for row in experiences],
            "counts": {
                "capabilities": len(capabilities),
                "experiences": len(experiences),
                "confirmed_capabilities": sum(
                    1 for row in capabilities if row.state == "confirmed"
                ),
                "confirmed_experiences": sum(
                    1 for row in experiences if row.state == "confirmed"
                ),
            },
            "rules_version": UNDERSTANDING_VERSION,
        }

    @staticmethod
    def capability_out(row) -> dict[str, Any]:
        return {
            "id": row.id,
            "document_id": row.company_document_id,
            "name": row.name,
            "description": row.description,
            "aliases": list(row.aliases or []),
            "page_index": row.page_index,
            "printed_label": row.printed_label,
            "excerpt": row.excerpt,
            "quote_verified": row.quote_verified,
            "is_ocr": row.is_ocr,
            "state": row.state,
            "origin": row.origin,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "confirmed_at": row.confirmed_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def experience_out(row) -> dict[str, Any]:
        return {
            "id": row.id,
            "document_id": row.company_document_id,
            "title": row.title,
            "client": row.client,
            "description": row.description,
            "work_family": row.work_family,
            "value_amount": row.value_amount,
            "currency": row.currency,
            "completed_on": row.completed_on,
            "page_index": row.page_index,
            "printed_label": row.printed_label,
            "excerpt": row.excerpt,
            "quote_verified": row.quote_verified,
            "is_ocr": row.is_ocr,
            "state": row.state,
            "origin": row.origin,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "confirmed_at": row.confirmed_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # ----------------------------------------------------------------- write
    def extract(
        self,
        *,
        document: CompanyDocument,
        pages: list[dict],
        company: Company | None = None,
        progress=None,
    ) -> UnderstandingResult:
        """Read the profile pages into cited capabilities and experiences."""

        if self.ai is None or not self.ai.configured:
            return self._finish(
                document,
                state="no_ai",
                error=(
                    "Add an AI key to read capabilities and experience from "
                    "this profile. The work words below were still read."
                ),
            )

        page_map = _Pages()
        for page in pages:
            if not page.get("has_text"):
                continue
            page_map.by_number[int(page["page_index"])] = page

        if not page_map.by_number:
            return self._finish(
                document,
                state="no_text_layer",
                error="No page text was found, so no capability could be cited.",
            )

        chunks = chunk_pages(
            list(page_map.by_number.values()),
            max_chars=self.settings.ai_extraction_max_chunk_chars,
            max_pages_per_chunk=self.settings.ai_extraction_max_pages_per_chunk,
        )
        chunks = chunks[: max(self.settings.ai_extraction_max_chunks, 1)]

        capabilities: dict[str, dict[str, Any]] = {}
        experiences: dict[str, dict[str, Any]] = {}
        dropped = 0
        failed = 0
        model = getattr(self.ai, "model", None)

        for chunk in chunks:
            if progress is not None:
                try:
                    progress()
                except Exception:  # noqa: BLE001 - a heartbeat is best effort
                    pass
            try:
                raw = self.ai.complete_json(
                    system=SYSTEM_PROMPT, user=_chunk_prompt(chunk)
                )
                answer = UnderstandingAnswer.model_validate(raw)
            except (AIError, ValueError, TypeError):
                failed += 1
                continue
            for item in answer.capabilities:
                row = _valid_capability(
                    item,
                    pages=page_map,
                    company_name=getattr(company, "name", None),
                    model=model,
                )
                if row is None:
                    dropped += 1
                    continue
                if row["normalized"] in capabilities:
                    dropped += 1
                    continue
                capabilities[row["normalized"]] = row
            for item in answer.experiences:
                row = _valid_experience(item, pages=page_map, model=model)
                if row is None:
                    dropped += 1
                    continue
                if row["normalized"] in experiences:
                    dropped += 1
                    continue
                experiences[row["normalized"]] = row

        if failed and not capabilities and not experiences:
            # A profile with no text and a failed call are different failures.
            # Keep the last good rows and say what happened.
            return self._finish(
                document,
                state="failed",
                error=(
                    "The profile reader could not be reached. The work words "
                    "below were still read; try re-reading the profile."
                ),
                chunks=len(chunks),
                failed_chunks=failed,
                dropped=dropped,
                model=model,
            )

        ordered_capabilities = list(capabilities.values())[:MAX_CAPABILITIES]
        ordered_experiences = list(experiences.values())[:MAX_EXPERIENCES]
        self.repository.replace_capabilities(
            document=document, rows=ordered_capabilities
        )
        self.repository.replace_experiences(
            document=document, rows=ordered_experiences
        )
        return self._finish(
            document,
            state="done",
            error=(
                "Some parts of the profile could not be read; the rest is "
                "shown."
                if failed
                else None
            ),
            chunks=len(chunks),
            failed_chunks=failed,
            dropped=dropped,
            model=model,
        )

    def _finish(
        self,
        document: CompanyDocument,
        *,
        state: str,
        error: str | None,
        chunks: int = 0,
        failed_chunks: int = 0,
        dropped: int = 0,
        model: str | None = None,
    ) -> UnderstandingResult:
        document.understanding_state = state
        document.understanding_error = error
        # A failed or unavailable re-read keeps the last good rows and their
        # counts: a transient provider failure must not erase a profile a human
        # already confirmed.
        if state in {"done", "no_text_layer"}:
            document.understanding_at = utcnow()
        document.capability_count = self.repository.count_capabilities(document.id)
        document.experience_count = self.repository.count_experiences(document.id)
        document.updated_at = utcnow()
        self.session.commit()
        return UnderstandingResult(
            state=state,
            error=error,
            capabilities=document.capability_count,
            experiences=document.experience_count,
            chunks=chunks,
            failed_chunks=failed_chunks,
            dropped=dropped,
            model=model,
        )

    # ---------------------------------------------------------------- edits
    def update_capability(
        self,
        *,
        company: Company,
        capability_id: uuid.UUID,
        payload: dict[str, Any],
        actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        row = self.repository.get_capability(
            company_id=company.id, capability_id=capability_id
        )
        if row is None:
            raise NotFound("Capability not found.")
        if "state" in payload and payload["state"] is not None:
            state = str(payload["state"])
            if state not in {"proposed", "confirmed", "rejected"}:
                raise BadRequest("Unknown capability state.")
            row.state = state
            row.confirmed_at = utcnow() if state == "confirmed" else None
            row.confirmed_by = (
                actor_user_id if state == "confirmed" else None
            )
        if payload.get("name") is not None:
            name = _clean(payload["name"], 160)
            if not name:
                raise BadRequest("A capability needs a name.")
            row.name = name
            row.normalized = normalize_for_match(name)[:160]
            row.origin = "human"
        if "description" in payload:
            row.description = _clean(payload["description"], MAX_DESCRIPTION_CHARS)
        if "aliases" in payload and payload["aliases"] is not None:
            row.aliases = _aliases(list(payload["aliases"]), row.name)
        row.updated_at = utcnow()
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise BadRequest(
                "Another capability from this profile already has that name."
            ) from exc
        self._refresh_generated_brief(
            company,
            source_document_id=row.company_document_id,
            actor_user_id=actor_user_id,
        )
        return self.capability_out(row)

    def update_experience(
        self,
        *,
        company: Company,
        experience_id: uuid.UUID,
        payload: dict[str, Any],
        actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        row = self.repository.get_experience(
            company_id=company.id, experience_id=experience_id
        )
        if row is None:
            raise NotFound("Experience not found.")
        if "state" in payload and payload["state"] is not None:
            state = str(payload["state"])
            if state not in {"proposed", "confirmed", "rejected"}:
                raise BadRequest("Unknown experience state.")
            row.state = state
            row.confirmed_at = utcnow() if state == "confirmed" else None
            row.confirmed_by = (
                actor_user_id if state == "confirmed" else None
            )
        if payload.get("title") is not None:
            title = _clean(payload["title"], 200)
            if not title:
                raise BadRequest("An experience needs a title.")
            row.title = title
            row.normalized = normalize_for_match(title)[:200]
            row.origin = "human"
        for key, limit in (
            ("client", 200),
            ("description", MAX_DESCRIPTION_CHARS),
            ("work_family", 200),
        ):
            if key in payload:
                setattr(row, key, _clean(payload[key], limit))
        row.updated_at = utcnow()
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise BadRequest(
                "Another experience from this profile already has that title."
            ) from exc
        self._refresh_generated_brief(
            company,
            source_document_id=row.company_document_id,
            actor_user_id=actor_user_id,
        )
        return self.experience_out(row)

    def _refresh_generated_brief(
        self,
        company: Company,
        *,
        source_document_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        """Keep a generated brief in step with a confirmation or edit.

        A human-edited brief is never overwritten. A generated one is a
        projection of the confirmed understanding, so confirming a capability
        must recompute it; otherwise the core terms a human just confirmed would
        not reach the fit ladder until they pressed "generate" again.
        """

        try:
            from app.services.company_brief import CompanyBriefService

            service = CompanyBriefService(self.session)
            current = service.current(company.id)
            if current is not None and current.source_kind == "human":
                return
            service.generate(
                company,
                actor_user_id=actor_user_id,
                source_document_id=source_document_id,
                source_kind="generated",
                commit=True,
            )
        except Exception:  # noqa: BLE001 - a brief is not worth failing an edit
            self.session.rollback()
