from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.assessment import CompanyTender
from app.models.workflow import Change, Reminder, TenderSnapshot


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest(self, tender_id: uuid.UUID) -> TenderSnapshot | None:
        return self.session.scalar(
            select(TenderSnapshot)
            .where(TenderSnapshot.tender_id == tender_id)
            .order_by(TenderSnapshot.captured_at.desc(), TenderSnapshot.id.desc())
            .limit(1)
        )

    def create(self, values: dict[str, Any]) -> TenderSnapshot:
        snapshot = TenderSnapshot(**values)
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def list_for_tender(
        self, tender_id: uuid.UUID, *, limit: int = 20, offset: int = 0
    ) -> list[TenderSnapshot]:
        return list(
            self.session.scalars(
                select(TenderSnapshot)
                .where(TenderSnapshot.tender_id == tender_id)
                .order_by(TenderSnapshot.captured_at.desc(), TenderSnapshot.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def count_for_tender(self, tender_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(TenderSnapshot.id)).where(
                    TenderSnapshot.tender_id == tender_id
                )
            )
            or 0
        )


class ChangeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_natural(
        self,
        *,
        tender_id: uuid.UUID,
        from_snapshot_id: uuid.UUID | None,
        to_snapshot_id: uuid.UUID,
        field_key: str,
    ) -> Change | None:
        statement = select(Change).where(
            Change.tender_id == tender_id,
            Change.to_snapshot_id == to_snapshot_id,
            Change.field_key == field_key,
        )
        if from_snapshot_id is None:
            statement = statement.where(Change.from_snapshot_id.is_(None))
        else:
            statement = statement.where(Change.from_snapshot_id == from_snapshot_id)
        return self.session.scalar(statement)

    def create_many(self, values: list[dict[str, Any]]) -> list[Change]:
        created: list[Change] = []
        for item in values:
            existing = self.find_natural(
                tender_id=item["tender_id"],
                from_snapshot_id=item.get("from_snapshot_id"),
                to_snapshot_id=item["to_snapshot_id"],
                field_key=item["field_key"],
            )
            if existing is not None:
                continue
            change = Change(**item)
            self.session.add(change)
            created.append(change)
        if created:
            self.session.flush()
        return created

    def get(self, change_id: uuid.UUID) -> Change | None:
        return self.session.get(Change, change_id)

    def list_for_tender(
        self,
        tender_id: uuid.UUID,
        *,
        review_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Change], int]:
        statement = select(Change).where(Change.tender_id == tender_id)
        if review_state:
            statement = statement.where(Change.review_state == review_state)
        total = int(
            self.session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            or 0
        )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    Change.detected_at.desc(), Change.id.desc()
                )
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return rows, total

    def list_for_company(
        self,
        company_id: uuid.UUID,
        *,
        saved_only: bool = True,
        review_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Change], int]:
        statement = (
            select(Change)
            .join(CompanyTender, CompanyTender.tender_id == Change.tender_id)
            .where(CompanyTender.company_id == company_id)
        )
        if saved_only:
            statement = statement.where(
                CompanyTender.saved_at.is_not(None),
                CompanyTender.dismissed_at.is_(None),
            )
        if review_state:
            statement = statement.where(Change.review_state == review_state)
        total = int(
            self.session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            or 0
        )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    Change.detected_at.desc(), Change.id.desc()
                )
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return rows, total

    def review(
        self,
        change: Change,
        *,
        review_state: str,
        reviewed_by: uuid.UUID,
        reviewed_at: datetime,
    ) -> Change:
        change.review_state = review_state
        change.reviewed_by = reviewed_by
        change.reviewed_at = reviewed_at
        self.session.flush()
        return change


class ReminderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, company_id: uuid.UUID, reminder_id: uuid.UUID) -> Reminder | None:
        return self.session.scalar(
            select(Reminder).where(
                Reminder.id == reminder_id,
                Reminder.company_id == company_id,
            )
        )

    def find_by_key(self, company_id: uuid.UUID, key: str) -> Reminder | None:
        return self.session.scalar(
            select(Reminder).where(
                Reminder.company_id == company_id, Reminder.key == key
            )
        )

    def upsert_generated(
        self,
        *,
        company_id: uuid.UUID,
        key: str,
        due_at: datetime,
        lead_days: int | None,
        tender_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
    ) -> Reminder:
        row = self.find_by_key(company_id, key)
        if row is None:
            row = Reminder(
                company_id=company_id,
                key=key,
                due_at=due_at,
                lead_days=lead_days,
                tender_id=tender_id,
                task_id=task_id,
                state="open",
            )
            self.session.add(row)
        else:
            changed = row.due_at != due_at
            row.tender_id = tender_id
            row.task_id = task_id
            row.lead_days = lead_days
            if changed:
                # A moved deadline is new work, so the old acknowledgement does
                # not carry over. The reminder reopens at its new time.
                row.due_at = due_at
                row.state = "open"
                row.sent_at = None
                row.acknowledged_at = None
                row.attempts = 0
                row.last_error = None
            row.updated_at = utcnow()
        self.session.flush()
        return row

    def delete_except(self, company_id: uuid.UUID, keys: set[str]) -> int:
        rows = list(
            self.session.scalars(
                select(Reminder).where(Reminder.company_id == company_id)
            ).all()
        )
        removed = 0
        for row in rows:
            if row.key not in keys:
                self.session.delete(row)
                removed += 1
        if removed:
            self.session.flush()
        return removed

    def list_for_company(
        self,
        company_id: uuid.UUID,
        *,
        state: str | None = None,
        tender_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Reminder], int]:
        statement = select(Reminder).where(Reminder.company_id == company_id)
        if state:
            statement = statement.where(Reminder.state == state)
        if tender_id:
            statement = statement.where(Reminder.tender_id == tender_id)
        total = int(
            self.session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            or 0
        )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    Reminder.state.asc(), Reminder.due_at.asc(), Reminder.id.asc()
                )
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return rows, total

    def due_for_company(
        self,
        company_id: uuid.UUID,
        now: datetime,
        *,
        limit: int = 100,
    ) -> list[Reminder]:
        return list(
            self.session.scalars(
                select(Reminder)
                .where(
                    Reminder.company_id == company_id,
                    Reminder.state == "open",
                    Reminder.acknowledged_at.is_(None),
                    Reminder.due_at <= now,
                )
                .order_by(Reminder.due_at.asc(), Reminder.id.asc())
                .limit(limit)
            ).all()
        )

    def claim_due(
        self, company_id: uuid.UUID, now: datetime
    ) -> Reminder | None:
        row = self.session.scalar(
            select(Reminder)
            .where(
                Reminder.company_id == company_id,
                Reminder.state == "open",
                Reminder.acknowledged_at.is_(None),
                Reminder.due_at <= now,
            )
            .order_by(Reminder.due_at.asc(), Reminder.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.state = "sent"
        row.sent_at = now
        row.attempts = (row.attempts or 0) + 1
        row.updated_at = now
        self.session.flush()
        return row

    def mark_failed(
        self, row: Reminder, *, error: str, now: datetime
    ) -> Reminder:
        row.state = "open"
        row.last_error = error[:1000]
        row.updated_at = now
        self.session.flush()
        return row

    def mark_acknowledged(
        self, row: Reminder, *, now: datetime
    ) -> Reminder:
        row.acknowledged_at = now
        row.updated_at = now
        self.session.flush()
        return row

    def dismiss(self, row: Reminder, *, now: datetime) -> Reminder:
        row.state = "dismissed"
        row.acknowledged_at = row.acknowledged_at or now
        row.updated_at = now
        self.session.flush()
        return row
