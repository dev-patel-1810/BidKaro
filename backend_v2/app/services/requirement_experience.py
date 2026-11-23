"""Map tender requirements against what the company has already delivered.

The question is deliberately narrow: *does the company's own confirmed
experience show this kind of work?* The model answers with one of four labels
per requirement - ``covered``, ``partial``, ``new`` or ``unknown`` - and names
the ids of the experiences or capabilities it used. Nothing else.

Guards:

* only human-confirmed capabilities and experiences are sent;
* the model may only return ids that were given to it, and a ``covered`` or
  ``partial`` answer with no valid id is downgraded to ``unknown``;
* the company-side citation is copied from the stored page, never from the
  model's words, so the user can open the page and read the line;
* a ``new`` answer is a prompt to look, not a fail. Eligibility stays with the
  deterministic assessment and gate.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, NotFound, ServiceUnavailable
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.integrations.ai import AIError, AIProvider
from app.models.corpus import Requirement, Tender
from app.models.identity import Company
from app.repositories.corpus import RequirementRepository, TenderRepository
from app.repositories.intelligence import stable_hash
from app.repositories.understanding import (
    CompanyUnderstandingRepository,
    RequirementExperienceRepository,
)
from app.services.corpus_rules import normalize_for_match, trim

MATCH_VERSION = "2026-11-18.1"
COVERAGE_VALUES = ("covered", "partial", "new", "unknown")
MAX_REQUIREMENT_CHARS = 600

SYSTEM_PROMPT = """You compare tender requirements with a company's own delivered work.
Return JSON only, in exactly this shape:
{"matches": [{"requirement_id": "...", "coverage": "covered",
  "experience_ids": ["..."], "capability_ids": ["..."]}]}

Coverage labels, all mandatory:
- "covered": one listed experience or capability explicitly shows the company
  has done this kind of work or delivered this kind of item/service.
- "partial": the company has related work, but it does not clearly meet this
  requirement's specific scope, quantity, type or scale.
- "new": the company's listed work contains nothing of this kind. This is a
  prompt to look, not a failure.
- "unknown": the requirement is not about the company's work at all (a form, a
  fee, a submission instruction, a legal undertaking), or the notice does not
  state enough to tell.

Rules, all mandatory:
- Use only ids from the lists given. Never invent an id.
- A capability alone can support "covered" only when it directly names the same
  work. A named project or product is stronger evidence than a capability.
- Never decide eligibility, compliance, pass/fail, money or deadlines. You are
  only saying whether the company's own work shows this before.
- One match per requirement, using the id exactly as given. No other text.
"""


class RequirementMatchAnswer(BaseModel):
    requirement_id: str = ""
    coverage: str = "unknown"
    experience_ids: list[str] = Field(default_factory=list)
    capability_ids: list[str] = Field(default_factory=list)


class RequirementMatchAnswerList(BaseModel):
    matches: list[RequirementMatchAnswer] = Field(default_factory=list)


def _coverage_reason(coverage: str) -> str:
    return {
        "covered": "Your confirmed experience shows this kind of work.",
        "partial": (
            "Your profile shows related work, but not this exact scope. "
            "Check the page before relying on it."
        ),
        "new": (
            "No project or product in your confirmed profile shows this kind "
            "of work. This is new work to look at, not a failure."
        ),
        "unknown": (
            "This requirement is not about your work, or the notice does not "
            "say enough to tell."
        ),
    }[coverage]


class RequirementExperienceService:
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
        self.requirements = RequirementRepository(session)
        self.tenders = TenderRepository(session)
        self.understanding = CompanyUnderstandingRepository(session)
        self.matches = RequirementExperienceRepository(session)

    # ------------------------------------------------------------------ read
    def listing(self, *, company: Company, tender_id: uuid.UUID) -> dict[str, Any]:
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        rows = self.matches.for_tender(company_id=company.id, tender_id=tender_id)
        capabilities = {
            row.id: row
            for row in self.understanding.capabilities_for_company(company.id)
        }
        experiences = {
            row.id: row
            for row in self.understanding.experiences_for_company(company.id)
        }
        items = [
            self.match_out(row, capabilities=capabilities, experiences=experiences)
            for row in rows.values()
        ]
        items.sort(key=lambda item: str(item["requirement_id"]))
        counts = {value: 0 for value in COVERAGE_VALUES}
        for item in items:
            counts[item["coverage"]] += 1
        return {"items": items, "counts": counts, "rules_version": MATCH_VERSION}

    @staticmethod
    def match_out(
        row, *, capabilities: dict, experiences: dict
    ) -> dict[str, Any]:
        return {
            "requirement_id": row.requirement_id,
            "coverage": row.coverage,
            "reason": row.reason,
            "capabilities": [
                capabilities[item].name
                for item in (row.capability_ids or [])
                if item in capabilities
            ],
            "experiences": [
                experiences[item].title
                for item in (row.experience_ids or [])
                if item in experiences
            ],
            "company_citation": (
                {
                    "document_id": row.company_document_id,
                    "page_index": row.company_page_index,
                    "excerpt": row.company_excerpt,
                }
                if row.company_excerpt
                else None
            ),
            "requirement_excerpt": row.requirement_excerpt,
            "model": row.model,
            "rules_version": row.prompt_version,
            "updated_at": row.updated_at,
        }

    # ----------------------------------------------------------------- write
    def map_tender(
        self, *, company: Company, tender_id: uuid.UUID
    ) -> dict[str, Any]:
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        capabilities = self.understanding.confirmed_capabilities(company.id)
        experiences = self.understanding.confirmed_experiences(company.id)
        if not capabilities and not experiences:
            raise BadRequest(
                "Confirm at least one capability or experience from your "
                "company profile before comparing it with requirements."
            )
        if self.ai is None or not self.ai.configured:
            raise ServiceUnavailable(
                "Add an AI key to compare requirements with your experience."
            )

        rows, _total = self.requirements.list_for_tender(
            tender_id,
            company_id=company.id,
            limit=max(self.settings.requirement_match_max_requirements, 1),
        )
        requirements = self._prioritise(rows)
        if not requirements:
            return self.listing(company=company, tender_id=tender_id)

        company_signature = self._company_signature(capabilities, experiences)
        hashes = {
            requirement.id: stable_hash(
                {
                    "version": MATCH_VERSION,
                    "company": company_signature,
                    "requirement": requirement.excerpt_hash,
                }
            )
            for requirement in requirements
        }
        existing = self.matches.for_requirements(
            company.id, [requirement.id for requirement in requirements]
        )
        pending = [
            requirement
            for requirement in requirements
            if existing.get(requirement.id) is None
            or existing[requirement.id].input_hash != hashes[requirement.id]
        ]

        if pending:
            answers = self._ask(
                capabilities=capabilities,
                experiences=experiences,
                requirements=pending,
            )
            self._store(
                company=company,
                tender_id=tender_id,
                requirements=pending,
                answers=answers,
                capabilities=capabilities,
                experiences=experiences,
                hashes=hashes,
            )
            self.session.commit()

        return self.listing(company=company, tender_id=tender_id)

    def _prioritise(self, rows: list[Requirement]) -> list[Requirement]:
        """Mandatory, work-bearing requirements first, then page order."""

        order = {
            "eligibility": 0,
            "submission": 1,
            "form": 1,
            "compliance": 2,
            "performance": 3,
        }

        def key(item: Requirement) -> tuple:
            return (
                0 if item.mandatory_state == "mandatory" else 1,
                order.get(item.category, 4),
                item.page_index,
                str(item.id),
            )

        return sorted(rows, key=key)

    def _company_signature(self, capabilities: list, experiences: list) -> dict:
        return {
            "capabilities": sorted(
                (
                    str(row.id),
                    row.normalized,
                    normalize_for_match(row.description or ""),
                )
                for row in capabilities
            ),
            "experiences": sorted(
                (
                    str(row.id),
                    row.normalized,
                    normalize_for_match(row.description or ""),
                    normalize_for_match(row.work_family or ""),
                )
                for row in experiences
            ),
        }

    def _ask(
        self,
        *,
        capabilities: list,
        experiences: list,
        requirements: list[Requirement],
    ) -> RequirementMatchAnswerList:
        capabilities_text = "\n".join(
            f"- id: {row.id}\n  name: {row.name}\n"
            f"  description: {row.description or '(none)'}\n"
            f"  aliases: {', '.join(row.aliases or []) or '(none)'}"
            for row in capabilities
        ) or "(none confirmed)"
        experiences_text = "\n".join(
            f"- id: {row.id}\n  title: {row.title}\n"
            f"  client: {row.client or '(not stated)'}\n"
            f"  work family: {row.work_family or '(not stated)'}\n"
            f"  description: {row.description or '(none)'}\n"
            f"  delivered: {row.completed_on.isoformat() if row.completed_on else '(not stated)'}"
            for row in experiences
        ) or "(none confirmed)"
        requirements_text = "\n\n".join(
            f"requirement_id: {row.id}\n"
            f"category: {row.category}\n"
            f"mandatory: {row.mandatory_state}\n"
            f"excerpt: {trim(row.excerpt, MAX_REQUIREMENT_CHARS)}"
            for row in requirements
        )
        user = (
            f"Company's confirmed capabilities:\n{capabilities_text}\n\n"
            f"Company's confirmed delivered work:\n{experiences_text}\n\n"
            f"Tender requirements:\n{requirements_text}"
        )
        try:
            raw = self.ai.complete_json(system=SYSTEM_PROMPT, user=user)
            return RequirementMatchAnswerList.model_validate(raw)
        except (AIError, ValueError, TypeError) as exc:
            raise ServiceUnavailable(
                "The AI comparison could not be reached. Try again later."
            ) from exc

    def _store(
        self,
        *,
        company: Company,
        tender_id: uuid.UUID,
        requirements: list[Requirement],
        answers: RequirementMatchAnswerList,
        capabilities: list,
        experiences: list,
        hashes: dict[uuid.UUID, str],
    ) -> None:
        by_key = {str(row.id): row for row in requirements}
        capability_map = {str(row.id): row for row in capabilities}
        experience_map = {str(row.id): row for row in experiences}
        model = getattr(self.ai, "model", None)
        seen: set[str] = set()

        for answer in answers.matches:
            key = (answer.requirement_id or "").strip()
            requirement = by_key.get(key)
            if requirement is None or key in seen:
                continue
            seen.add(key)
            coverage = answer.coverage if answer.coverage in COVERAGE_VALUES else "unknown"
            experience_rows = [
                experience_map[item]
                for item in answer.experience_ids
                if item in experience_map
            ]
            capability_rows = [
                capability_map[item]
                for item in answer.capability_ids
                if item in capability_map
            ]
            # A labelled match must name the company's own row. Never accept a
            # "covered" that points at nothing.
            if coverage in {"covered", "partial"} and not (
                experience_rows or capability_rows
            ):
                coverage = "unknown"
            source = (
                experience_rows[0]
                if experience_rows
                else capability_rows[0]
                if capability_rows
                else None
            )
            self.matches.upsert(
                company_id=company.id,
                tender_id=tender_id,
                requirement_id=requirement.id,
                values={
                    "coverage": coverage,
                    "experience_ids": [
                        row.id for row in experience_rows if coverage in {"covered", "partial"}
                    ],
                    "capability_ids": [
                        row.id for row in capability_rows if coverage in {"covered", "partial"}
                    ],
                    "company_document_id": (
                        source.company_document_id if source is not None else None
                    ),
                    "company_page_index": (
                        source.page_index if source is not None else None
                    ),
                    "company_excerpt": (
                        source.excerpt if source is not None else None
                    ),
                    "requirement_excerpt": requirement.excerpt,
                    "reason": _coverage_reason(coverage),
                    "model": model,
                    "prompt_version": MATCH_VERSION,
                    "input_hash": hashes[requirement.id],
                },
            )

        # A requirement the reader skipped is "unknown", not "new": silence is
        # not evidence that the company has never done the work.
        for key, requirement in by_key.items():
            if key in seen:
                continue
            self.matches.upsert(
                company_id=company.id,
                tender_id=tender_id,
                requirement_id=requirement.id,
                values={
                    "coverage": "unknown",
                    "experience_ids": [],
                    "capability_ids": [],
                    "company_document_id": None,
                    "company_page_index": None,
                    "company_excerpt": None,
                    "requirement_excerpt": requirement.excerpt,
                    "reason": _coverage_reason("unknown"),
                    "model": model,
                    "prompt_version": MATCH_VERSION,
                    "input_hash": hashes[requirement.id],
                },
            )

    # --------------------------------------------------------------- helpers
    def out_for_requirement(
        self, *, company: Company, requirement_id: uuid.UUID
    ) -> dict[str, Any] | None:
        rows = self.matches.for_requirements(company.id, [requirement_id])
        row = rows.get(requirement_id)
        if row is None:
            return None
        return {
            "coverage": row.coverage,
            "reason": row.reason,
            "company_excerpt": row.company_excerpt,
            "company_page_index": row.company_page_index,
            "company_document_id": row.company_document_id,
            "updated_at": row.updated_at,
        }
