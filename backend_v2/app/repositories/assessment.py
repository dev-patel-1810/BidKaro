from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.time import utcnow
from app.models.assessment import (
    AssessmentCriterion,
    ChecklistTask,
    CompanyTender,
    EvidenceMatch,
    FitAssessment,
)
from app.models.identity import CompanyDocument, CompanyDocumentVersion


def latest_version(document: CompanyDocument) -> CompanyDocumentVersion | None:
    """The newest uploaded bytes, never a guessed parent-document link."""

    if not document.versions:
        return None
    return max(
        document.versions,
        key=lambda item: (item.uploaded_at, item.created_at, str(item.id)),
    )


class CompanyTenderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, company_id: uuid.UUID, tender_id: uuid.UUID) -> CompanyTender | None:
        return self.session.scalar(
            select(CompanyTender).where(
                CompanyTender.company_id == company_id,
                CompanyTender.tender_id == tender_id,
            )
        )

    def upsert(
        self,
        *,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        values: dict[str, Any],
    ) -> CompanyTender:
        row = self.get(company_id, tender_id)
        if row is None:
            row = CompanyTender(company_id=company_id, tender_id=tender_id)
            self.session.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def list_active(self, company_id: uuid.UUID) -> list[CompanyTender]:
        """Saved or bidding tenders whose company has not dismissed them."""

        return list(
            self.session.scalars(
                select(CompanyTender)
                .where(
                    CompanyTender.company_id == company_id,
                    CompanyTender.dismissed_at.is_(None),
                    (
                        CompanyTender.saved_at.is_not(None)
                        | CompanyTender.bidding_at.is_not(None)
                    ),
                )
                .order_by(CompanyTender.created_at.asc())
            ).all()
        )

    def list_saved(self, company_id: uuid.UUID) -> list[CompanyTender]:
        return list(
            self.session.scalars(
                select(CompanyTender)
                .where(
                    CompanyTender.company_id == company_id,
                    CompanyTender.saved_at.is_not(None),
                    CompanyTender.dismissed_at.is_(None),
                )
                .order_by(CompanyTender.saved_at.desc())
            ).all()
        )


class CompanyDocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_company(self, company_id: uuid.UUID) -> list[CompanyDocument]:
        return list(
            self.session.scalars(
                select(CompanyDocument)
                .where(
                    CompanyDocument.company_id == company_id,
                    CompanyDocument.deleted_at.is_(None),
                )
                .options(selectinload(CompanyDocument.versions))
                .order_by(CompanyDocument.created_at.asc())
            ).all()
        )

    def get(
        self, company_id: uuid.UUID, document_id: uuid.UUID
    ) -> CompanyDocument | None:
        return self.session.scalar(
            select(CompanyDocument)
            .where(
                CompanyDocument.id == document_id,
                CompanyDocument.company_id == company_id,
                CompanyDocument.deleted_at.is_(None),
            )
            .options(selectinload(CompanyDocument.versions))
        )

    def get_version(
        self, company_id: uuid.UUID, version_id: uuid.UUID
    ) -> CompanyDocumentVersion | None:
        return self.session.scalar(
            select(CompanyDocumentVersion)
            .join(
                CompanyDocument,
                CompanyDocumentVersion.company_document_id == CompanyDocument.id,
            )
            .where(
                CompanyDocumentVersion.id == version_id,
                CompanyDocument.company_id == company_id,
                CompanyDocument.deleted_at.is_(None),
            )
            .options(joinedload(CompanyDocumentVersion.document))
        )


class EvidenceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, company_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> EvidenceMatch | None:
        return self.session.scalar(
            select(EvidenceMatch)
            .options(
                joinedload(EvidenceMatch.version).joinedload(
                    CompanyDocumentVersion.document
                )
            )
            .where(
                EvidenceMatch.company_id == company_id,
                EvidenceMatch.requirement_id == requirement_id,
            )
        )

    def for_requirements(
        self, company_id: uuid.UUID, requirement_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, EvidenceMatch]:
        if not requirement_ids:
            return {}
        rows = self.session.scalars(
            select(EvidenceMatch)
            .options(
                joinedload(EvidenceMatch.version).joinedload(
                    CompanyDocumentVersion.document
                )
            )
            .where(
                EvidenceMatch.company_id == company_id,
                EvidenceMatch.requirement_id.in_(requirement_ids),
            )
        ).all()
        return {row.requirement_id: row for row in rows}

    def upsert(
        self,
        *,
        company_id: uuid.UUID,
        requirement_id: uuid.UUID,
        version_id: uuid.UUID,
        status: str,
        reason: str | None,
        confirmed_by: uuid.UUID | None,
        confirmed_at: datetime | None,
    ) -> EvidenceMatch:
        row = self.get(company_id, requirement_id)
        if row is None:
            row = EvidenceMatch(
                company_id=company_id,
                requirement_id=requirement_id,
                company_document_version_id=version_id,
                status=status,
                reason=reason,
                confirmed_by=confirmed_by,
                confirmed_at=confirmed_at,
            )
            self.session.add(row)
        else:
            row.company_document_version_id = version_id
            row.status = status
            row.reason = reason
            row.confirmed_by = confirmed_by
            row.confirmed_at = confirmed_at
            row.updated_at = utcnow()
        self.session.flush()
        return row

    def delete(self, company_id: uuid.UUID, requirement_id: uuid.UUID) -> None:
        row = self.get(company_id, requirement_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def recompute_for_document(self, document_id: uuid.UUID, today: date) -> int:
        """Expire confirmations pinned to a document that is no longer valid.

        The match points at a version, but validity (expiry, rejected status,
        soft delete) belongs to the parent document. Expiring clears the human
        confirmation so the gate cannot keep passing on bytes that no longer
        prove the requirement.
        """

        rows = list(
            self.session.scalars(
                select(EvidenceMatch)
                .join(
                    CompanyDocumentVersion,
                    EvidenceMatch.company_document_version_id
                    == CompanyDocumentVersion.id,
                )
                .options(
                    joinedload(EvidenceMatch.version).joinedload(
                        CompanyDocumentVersion.document
                    )
                )
                .where(CompanyDocumentVersion.company_document_id == document_id)
            ).all()
        )
        changed = 0
        for row in rows:
            document = row.version.document if row.version else None
            if document is None:
                continue
            invalid = (
                document.deleted_at is not None
                or document.verification_status in {"rejected", "expired"}
                or (document.expires_on is not None and document.expires_on < today)
            )
            if not invalid:
                continue
            if (
                row.status != "expired"
                or row.confirmed_by is not None
                or row.confirmed_at is not None
            ):
                row.status = "expired"
                row.reason = "The evidence document expired or was removed; confirm current evidence."
                row.confirmed_by = None
                row.confirmed_at = None
                row.updated_at = utcnow()
                changed += 1
        if changed:
            self.session.flush()
        return changed


class AssessmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self,
        *,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        score: float,
        components: dict,
        gate: str,
        gate_reason: str,
        assessed_at: datetime,
    ) -> FitAssessment:
        row = self.session.scalar(
            select(FitAssessment).where(
                FitAssessment.company_id == company_id,
                FitAssessment.tender_id == tender_id,
            )
        )
        if row is None:
            row = FitAssessment(
                company_id=company_id,
                tender_id=tender_id,
                score=Decimal(str(score)),
                components=components,
                gate=gate,
                gate_reason=gate_reason,
                assessed_at=assessed_at,
            )
            self.session.add(row)
        else:
            row.score = Decimal(str(score))
            row.components = components
            row.gate = gate
            row.gate_reason = gate_reason
            row.assessed_at = assessed_at
        self.session.flush()
        return row

    def active_criteria(
        self, company_id: uuid.UUID, tender_id: uuid.UUID
    ) -> list[AssessmentCriterion]:
        return list(
            self.session.scalars(
                select(AssessmentCriterion)
                .where(
                    AssessmentCriterion.company_id == company_id,
                    AssessmentCriterion.tender_id == tender_id,
                    AssessmentCriterion.is_active.is_(True),
                )
                .order_by(AssessmentCriterion.created_at.asc())
            ).all()
        )

    def sync_criteria(
        self,
        *,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        items: list[dict[str, Any]],
    ) -> list[AssessmentCriterion]:
        """Replace the active criterion set without deleting its history."""

        existing = {
            row.requirement_id: row
            for row in self.active_criteria(company_id, tender_id)
        }
        keep: set[uuid.UUID] = set()
        result: list[AssessmentCriterion] = []
        for item in items:
            requirement_id = item["requirement_id"]
            keep.add(requirement_id)
            row = existing.get(requirement_id)
            if row is None:
                row = AssessmentCriterion(
                    company_id=company_id,
                    tender_id=tender_id,
                    requirement_id=requirement_id,
                    criterion_text=item["criterion_text"],
                    verdict=item["verdict"],
                    reason=item.get("reason"),
                    evidence_match_id=item.get("evidence_match_id"),
                    is_active=True,
                )
                self.session.add(row)
            else:
                row.criterion_text = item["criterion_text"]
                row.verdict = item["verdict"]
                row.reason = item.get("reason")
                row.evidence_match_id = item.get("evidence_match_id")
                row.updated_at = utcnow()
            result.append(row)
        for requirement_id, row in existing.items():
            if requirement_id not in keep:
                row.is_active = False
                row.updated_at = utcnow()
        self.session.flush()
        return result


class ChecklistRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, company_id: uuid.UUID, task_id: uuid.UUID
    ) -> ChecklistTask | None:
        return self.session.scalar(
            select(ChecklistTask).where(
                ChecklistTask.id == task_id,
                ChecklistTask.company_id == company_id,
            )
        )

    def find_generated(
        self,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        requirement_id: uuid.UUID,
        phase: str,
    ) -> ChecklistTask | None:
        return self.session.scalar(
            select(ChecklistTask).where(
                ChecklistTask.company_id == company_id,
                ChecklistTask.tender_id == tender_id,
                ChecklistTask.requirement_id == requirement_id,
                ChecklistTask.phase == phase,
            )
        )

    def list_for_tender(
        self,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        *,
        phase: str | None = None,
    ) -> list[ChecklistTask]:
        statement = select(ChecklistTask).where(
            ChecklistTask.company_id == company_id,
            ChecklistTask.tender_id == tender_id,
        )
        if phase is not None:
            statement = statement.where(ChecklistTask.phase == phase)
        statement = statement.order_by(
            ChecklistTask.phase.desc(),
            ChecklistTask.due_at.asc().nulls_last(),
            ChecklistTask.created_at.asc(),
        )
        return list(self.session.scalars(statement).all())

    def list_for_company(
        self, company_id: uuid.UUID, *, limit: int = 500
    ) -> list[ChecklistTask]:
        return list(
            self.session.scalars(
                select(ChecklistTask)
                .where(ChecklistTask.company_id == company_id)
                .order_by(
                    ChecklistTask.due_at.asc().nulls_last(),
                    ChecklistTask.created_at.asc(),
                )
                .limit(limit)
            ).all()
        )

    def count_for_tender(self, company_id: uuid.UUID, tender_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(ChecklistTask.id)).where(
                    ChecklistTask.company_id == company_id,
                    ChecklistTask.tender_id == tender_id,
                )
            )
            or 0
        )

    def count_open(self, company_id: uuid.UUID, tender_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(ChecklistTask.id)).where(
                    ChecklistTask.company_id == company_id,
                    ChecklistTask.tender_id == tender_id,
                    ChecklistTask.status.in_(
                        ["open", "in_progress", "blocked"]
                    ),
                )
            )
            or 0
        )

    def create(self, values: dict[str, Any]) -> ChecklistTask:
        task = ChecklistTask(**values)
        self.session.add(task)
        self.session.flush()
        return task

    def create_generated(
        self, values: dict[str, Any]
    ) -> tuple[ChecklistTask, bool]:
        """Create once per (company, tender, requirement, phase).

        An existing task is returned untouched: regenerating must never wipe a
        human's status, owner or due date.
        """

        existing = self.find_generated(
            values["company_id"],
            values["tender_id"],
            values["requirement_id"],
            values["phase"],
        )
        if existing is not None:
            return existing, False
        try:
            with self.session.begin_nested():
                task = self.create(values)
            return task, True
        except IntegrityError:
            existing = self.find_generated(
                values["company_id"],
                values["tender_id"],
                values["requirement_id"],
                values["phase"],
            )
            if existing is None:
                raise
            return existing, False

    def update(self, task: ChecklistTask, values: dict[str, Any]) -> ChecklistTask:
        for key, value in values.items():
            setattr(task, key, value)
        task.updated_at = utcnow()
        self.session.flush()
        return task
