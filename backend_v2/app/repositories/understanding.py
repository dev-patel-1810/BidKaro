"""Storage for the company's understanding, similarity answers and matches.

Every read that feeds discovery is company-scoped and ignores a soft-deleted
document or a document that is not an about-company profile. That keeps a
removed profile from still suggesting work, without touching the document's
own eligibility evidence.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.identity import CompanyDocument
from app.models.understanding import (
    CompanyBrief,
    CompanyCapability,
    CompanyExperience,
    RequirementExperienceMatch,
    TenderSimilarityJudgment,
)

ABOUT_COMPANY_TYPE = "about_company"


class CompanyUnderstandingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- reads
    def capabilities_for_document(
        self, document_id: uuid.UUID
    ) -> list[CompanyCapability]:
        return list(
            self.session.scalars(
                select(CompanyCapability)
                .where(CompanyCapability.company_document_id == document_id)
                .order_by(CompanyCapability.state, CompanyCapability.name)
            )
        )

    def experiences_for_document(
        self, document_id: uuid.UUID
    ) -> list[CompanyExperience]:
        return list(
            self.session.scalars(
                select(CompanyExperience)
                .where(CompanyExperience.company_document_id == document_id)
                .order_by(CompanyExperience.state, CompanyExperience.title)
            )
        )

    def capabilities_for_company(
        self, company_id: uuid.UUID, *, states: tuple[str, ...] | None = None
    ) -> list[CompanyCapability]:
        statement = (
            select(CompanyCapability)
            .join(
                CompanyDocument,
                CompanyDocument.id == CompanyCapability.company_document_id,
            )
            .where(
                CompanyCapability.company_id == company_id,
                CompanyDocument.deleted_at.is_(None),
                CompanyDocument.document_type == ABOUT_COMPANY_TYPE,
            )
        )
        if states:
            statement = statement.where(CompanyCapability.state.in_(states))
        return list(
            self.session.scalars(
                statement.order_by(
                    CompanyCapability.state, CompanyCapability.name
                )
            )
        )

    def experiences_for_company(
        self, company_id: uuid.UUID, *, states: tuple[str, ...] | None = None
    ) -> list[CompanyExperience]:
        statement = (
            select(CompanyExperience)
            .join(
                CompanyDocument,
                CompanyDocument.id == CompanyExperience.company_document_id,
            )
            .where(
                CompanyExperience.company_id == company_id,
                CompanyDocument.deleted_at.is_(None),
                CompanyDocument.document_type == ABOUT_COMPANY_TYPE,
            )
        )
        if states:
            statement = statement.where(CompanyExperience.state.in_(states))
        return list(
            self.session.scalars(
                statement.order_by(
                    CompanyExperience.state, CompanyExperience.title
                )
            )
        )

    def confirmed_capabilities(self, company_id: uuid.UUID) -> list[CompanyCapability]:
        return self.capabilities_for_company(company_id, states=("confirmed",))

    def confirmed_experiences(self, company_id: uuid.UUID) -> list[CompanyExperience]:
        return self.experiences_for_company(company_id, states=("confirmed",))

    def get_capability(
        self, *, company_id: uuid.UUID, capability_id: uuid.UUID
    ) -> CompanyCapability | None:
        return self.session.scalar(
            select(CompanyCapability).where(
                CompanyCapability.id == capability_id,
                CompanyCapability.company_id == company_id,
            )
        )

    def get_experience(
        self, *, company_id: uuid.UUID, experience_id: uuid.UUID
    ) -> CompanyExperience | None:
        return self.session.scalar(
            select(CompanyExperience).where(
                CompanyExperience.id == experience_id,
                CompanyExperience.company_id == company_id,
            )
        )

    def understanding_documents(self, company_id: uuid.UUID) -> list[CompanyDocument]:
        return list(
            self.session.scalars(
                select(CompanyDocument)
                .where(
                    CompanyDocument.company_id == company_id,
                    CompanyDocument.deleted_at.is_(None),
                    CompanyDocument.document_type == ABOUT_COMPANY_TYPE,
                )
                .order_by(CompanyDocument.created_at.desc())
            )
        )

    def count_capabilities(
        self, document_id: uuid.UUID, *, states: tuple[str, ...] | None = None
    ) -> int:
        statement = select(func.count()).select_from(CompanyCapability).where(
            CompanyCapability.company_document_id == document_id
        )
        if states:
            statement = statement.where(CompanyCapability.state.in_(states))
        return int(self.session.scalar(statement) or 0)

    def count_experiences(
        self, document_id: uuid.UUID, *, states: tuple[str, ...] | None = None
    ) -> int:
        statement = select(func.count()).select_from(CompanyExperience).where(
            CompanyExperience.company_document_id == document_id
        )
        if states:
            statement = statement.where(CompanyExperience.state.in_(states))
        return int(self.session.scalar(statement) or 0)

    # --------------------------------------------------------------- writes
    def replace_capabilities(
        self,
        *,
        document: CompanyDocument,
        rows: list[dict[str, Any]],
    ) -> int:
        """Upsert the read capabilities, keeping a human decision.

        A confirmed row is never deleted because a later read dropped it: the
        human said the company does that work. Its wording and citation are
        refreshed when the new read still names it.
        """

        existing = {
            row.normalized: row for row in self.capabilities_for_document(document.id)
        }
        seen: set[str] = set()
        for item in rows:
            normalized = item["normalized"]
            if normalized in seen:
                continue
            seen.add(normalized)
            row = existing.get(normalized)
            if row is None:
                self.session.add(
                    CompanyCapability(
                        company_id=document.company_id,
                        company_document_id=document.id,
                        **item,
                    )
                )
                continue
            row.name = item["name"]
            row.description = item.get("description")
            row.aliases = list(item.get("aliases") or [])
            row.page_index = item["page_index"]
            row.printed_label = item.get("printed_label")
            row.excerpt = item["excerpt"]
            row.quote_verified = item.get("quote_verified", True)
            row.is_ocr = item.get("is_ocr", False)
            row.model = item.get("model")
            row.prompt_version = item.get("prompt_version")
        for normalized, row in existing.items():
            if normalized in seen or row.state == "confirmed":
                continue
            self.session.delete(row)
        return len(seen)

    def replace_experiences(
        self,
        *,
        document: CompanyDocument,
        rows: list[dict[str, Any]],
    ) -> int:
        existing = {
            row.normalized: row for row in self.experiences_for_document(document.id)
        }
        seen: set[str] = set()
        for item in rows:
            normalized = item["normalized"]
            if normalized in seen:
                continue
            seen.add(normalized)
            row = existing.get(normalized)
            if row is None:
                self.session.add(
                    CompanyExperience(
                        company_id=document.company_id,
                        company_document_id=document.id,
                        **item,
                    )
                )
                continue
            row.title = item["title"]
            row.client = item.get("client")
            row.description = item.get("description")
            row.work_family = item.get("work_family")
            row.value_amount = item.get("value_amount")
            row.currency = item.get("currency") or "INR"
            row.completed_on = item.get("completed_on")
            row.page_index = item["page_index"]
            row.printed_label = item.get("printed_label")
            row.excerpt = item["excerpt"]
            row.quote_verified = item.get("quote_verified", True)
            row.is_ocr = item.get("is_ocr", False)
            row.model = item.get("model")
            row.prompt_version = item.get("prompt_version")
        for normalized, row in existing.items():
            if normalized in seen or row.state == "confirmed":
                continue
            self.session.delete(row)
        return len(seen)

    def delete_for_document(self, document_id: uuid.UUID) -> None:
        self.session.execute(
            delete(CompanyCapability).where(
                CompanyCapability.company_document_id == document_id
            )
        )
        self.session.execute(
            delete(CompanyExperience).where(
                CompanyExperience.company_document_id == document_id
            )
        )


class CompanyBriefRepository:
    """Versioned company briefs. Postgres is the source of truth."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self, company_id: uuid.UUID) -> CompanyBrief | None:
        return self.session.scalar(
            select(CompanyBrief).where(
                CompanyBrief.company_id == company_id,
                CompanyBrief.is_current.is_(True),
            )
        )

    def get(self, *, company_id: uuid.UUID, brief_id: uuid.UUID) -> CompanyBrief | None:
        return self.session.scalar(
            select(CompanyBrief).where(
                CompanyBrief.id == brief_id,
                CompanyBrief.company_id == company_id,
            )
        )

    def versions(
        self, company_id: uuid.UUID, *, limit: int = 50
    ) -> list[CompanyBrief]:
        return list(
            self.session.scalars(
                select(CompanyBrief)
                .where(CompanyBrief.company_id == company_id)
                .order_by(CompanyBrief.version.desc())
                .limit(limit)
            )
        )

    def next_version(self, company_id: uuid.UUID) -> int:
        latest = self.session.scalar(
            select(func.max(CompanyBrief.version)).where(
                CompanyBrief.company_id == company_id
            )
        )
        return int(latest or 0) + 1

    def create(
        self,
        *,
        company_id: uuid.UUID,
        payload: dict[str, Any],
        source_kind: str,
        source_document_id: uuid.UUID | None = None,
        prompt_version: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> CompanyBrief:
        version = self.next_version(company_id)
        self.session.execute(
            update(CompanyBrief)
            .where(
                CompanyBrief.company_id == company_id,
                CompanyBrief.is_current.is_(True),
            )
            .values(is_current=False, updated_at=utcnow())
        )
        row = CompanyBrief(
            company_id=company_id,
            version=version,
            is_current=True,
            source_document_id=source_document_id,
            source_kind=source_kind,
            payload=payload,
            prompt_version=prompt_version,
            created_by=created_by,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def activate(self, *, company_id: uuid.UUID, brief_id: uuid.UUID) -> CompanyBrief | None:
        target = self.get(company_id=company_id, brief_id=brief_id)
        if target is None:
            return None
        self.session.execute(
            update(CompanyBrief)
            .where(
                CompanyBrief.company_id == company_id,
                CompanyBrief.is_current.is_(True),
            )
            .values(is_current=False, updated_at=utcnow())
        )
        target.is_current = True
        target.updated_at = utcnow()
        self.session.flush()
        return target


class SimilarityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, *, company_id: uuid.UUID, tender_id: uuid.UUID
    ) -> TenderSimilarityJudgment | None:
        return self.session.scalar(
            select(TenderSimilarityJudgment).where(
                TenderSimilarityJudgment.company_id == company_id,
                TenderSimilarityJudgment.tender_id == tender_id,
            )
        )

    def get_many(
        self, company_id: uuid.UUID, tender_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, TenderSimilarityJudgment]:
        if not tender_ids:
            return {}
        rows = self.session.scalars(
            select(TenderSimilarityJudgment).where(
                TenderSimilarityJudgment.company_id == company_id,
                TenderSimilarityJudgment.tender_id.in_(tender_ids),
            )
        )
        return {row.tender_id: row for row in rows}

    def upsert(
        self,
        *,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        values: dict[str, Any],
    ) -> TenderSimilarityJudgment:
        row = self.get(company_id=company_id, tender_id=tender_id)
        if row is None:
            row = TenderSimilarityJudgment(
                company_id=company_id, tender_id=tender_id, **values
            )
            self.session.add(row)
            return row
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        return row


class RequirementExperienceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def for_tender(
        self, *, company_id: uuid.UUID, tender_id: uuid.UUID
    ) -> dict[uuid.UUID, RequirementExperienceMatch]:
        rows = self.session.scalars(
            select(RequirementExperienceMatch).where(
                RequirementExperienceMatch.company_id == company_id,
                RequirementExperienceMatch.tender_id == tender_id,
            )
        )
        return {row.requirement_id: row for row in rows}

    def for_requirements(
        self, company_id: uuid.UUID, requirement_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, RequirementExperienceMatch]:
        if not requirement_ids:
            return {}
        rows = self.session.scalars(
            select(RequirementExperienceMatch).where(
                RequirementExperienceMatch.company_id == company_id,
                RequirementExperienceMatch.requirement_id.in_(requirement_ids),
            )
        )
        return {row.requirement_id: row for row in rows}

    def upsert(
        self,
        *,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        requirement_id: uuid.UUID,
        values: dict[str, Any],
    ) -> RequirementExperienceMatch:
        row = self.session.scalar(
            select(RequirementExperienceMatch).where(
                RequirementExperienceMatch.company_id == company_id,
                RequirementExperienceMatch.requirement_id == requirement_id,
            )
        )
        if row is None:
            row = RequirementExperienceMatch(
                company_id=company_id,
                tender_id=tender_id,
                requirement_id=requirement_id,
                **values,
            )
            self.session.add(row)
            return row
        for key, value in values.items():
            setattr(row, key, value)
        row.tender_id = tender_id
        row.updated_at = utcnow()
        return row
