from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.time import utcnow
from app.models.assessment import ChecklistTask, CompanyTender
from app.models.identity import Company, User
from app.models.workflow import Reminder
from app.repositories.assessment import ChecklistRepository, CompanyTenderRepository
from app.repositories.corpus import TenderRepository
from app.repositories.identity import AuditRepository
from app.repositories.workflow import ReminderRepository
from app.services.email import EmailService

CLOSED_TASK_STATUSES = {"done", "not_applicable"}
DEFAULT_LEAD_DAYS = (7, 3, 1)


def _lead_days(company: Company) -> list[int]:
    days = sorted(
        {
            int(day)
            for day in (company.reminder_lead_days or [])
            if 0 <= int(day) <= 365
        },
        reverse=True,
    )
    return days or list(DEFAULT_LEAD_DAYS)


def _date_text(value: datetime | None) -> str:
    if value is None:
        return "not stated"
    return value.astimezone().strftime("%d %b %Y, %H:%M %Z")


def _short_date(value: datetime | None) -> str:
    if value is None:
        return "not stated"
    return value.astimezone().strftime("%d %b %Y")


class ReminderService:
    def __init__(
        self, session: Session, *, email: EmailService | None = None
    ) -> None:
        self.session = session
        self.reminders = ReminderRepository(session)
        self.company_tenders = CompanyTenderRepository(session)
        self.tasks = ChecklistRepository(session)
        self.tenders = TenderRepository(session)
        self.audit = AuditRepository(session)
        self.email = email or EmailService()

    # -- derivation -------------------------------------------------------

    def sync(self, company: Company, *, now: datetime | None = None) -> int:
        """Derive the company's live reminders, preserving human decisions.

        A reminder is keyed by its source and lead time, so the same deadline
        re-derives the same row. When a due date moves, the row reopens; when a
        source disappears, its reminder is removed.
        """

        now = now or utcnow()
        leads = _lead_days(company)
        keys: set[str] = set()

        active_rows = self.company_tenders.list_active(company.id)
        for company_tender in active_rows:
            tender = self.tenders.get(company_tender.tender_id)
            if tender is None or tender.closing_at is None:
                continue
            if tender.closing_at <= now:
                continue
            for lead in leads:
                key = f"tender:{tender.id}:deadline:{lead}"
                keys.add(key)
                self.reminders.upsert_generated(
                    company_id=company.id,
                    key=key,
                    due_at=tender.closing_at - timedelta(days=lead),
                    lead_days=lead,
                    tender_id=tender.id,
                    task_id=None,
                )

        dismissed = {
            row.tender_id
            for row in self.session.scalars(
                select(CompanyTender).where(
                    CompanyTender.company_id == company.id,
                    CompanyTender.dismissed_at.is_not(None),
                )
            ).all()
        }
        for task in self.tasks.list_for_company(company.id):
            if task.status in CLOSED_TASK_STATUSES or task.due_at is None:
                continue
            if task.tender_id in dismissed:
                continue
            # Tasks carry their own due date and are already company-scoped, so
            # a manual task can produce a reminder even when its tender was
            # never saved.
            for lead in leads:
                key = f"task:{task.id}:due:{lead}"
                keys.add(key)
                self.reminders.upsert_generated(
                    company_id=company.id,
                    key=key,
                    due_at=task.due_at - timedelta(days=lead),
                    lead_days=lead,
                    tender_id=task.tender_id,
                    task_id=task.id,
                )

        self.reminders.delete_except(company.id, keys)
        self.session.commit()
        return len(keys)

    # -- reads ------------------------------------------------------------

    def list(
        self,
        company: Company,
        *,
        state: str | None = None,
        tender_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
        sync: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        if sync:
            self.sync(company)
        rows, total = self.reminders.list_for_company(
            company.id,
            state=state,
            tender_id=tender_id,
            limit=limit,
            offset=offset,
        )
        return [self._out(row) for row in rows], total

    def _out(self, row: Reminder) -> dict[str, Any]:
        if row.key.startswith("tender:") and row.tender_id is not None:
            tender = self.tenders.get(row.tender_id, include_merged=True)
            title = tender.work_title if tender and tender.work_title else "Tender deadline"
            closing = _short_date(tender.closing_at) if tender else "not stated"
            body = (
                f"The tender closes on {closing}. "
                + ("Source: " + tender.source_url if tender and tender.source_url else "")
            ).strip()
            return {
                "id": row.id,
                "company_id": row.company_id,
                "tender_id": row.tender_id,
                "task_id": row.task_id,
                "key": row.key,
                "type": "deadline",
                "title": title,
                "body": body,
                "href": f"/app/tenders/{row.tender_id}",
                "due_at": row.due_at,
                "lead_days": row.lead_days,
                "state": row.state,
                "sent_at": row.sent_at,
                "acknowledged_at": row.acknowledged_at,
                "is_acknowledged": row.acknowledged_at is not None,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "created_at": row.created_at,
            }

        task = (
            self.session.get(ChecklistTask, row.task_id)
            if row.task_id is not None
            else None
        )
        title = task.title if task is not None else "Checklist task due"
        pieces = [f"Due {_date_text(task.due_at) if task else 'not stated'}."]
        if task is not None and task.evidence_check:
            pieces.append("Evidence: " + task.evidence_check)
        if task is not None and task.phase == "post_award":
            pieces.append("Post-award obligation.")
        return {
            "id": row.id,
            "company_id": row.company_id,
            "tender_id": row.tender_id,
            "task_id": row.task_id,
            "key": row.key,
            "type": "task",
            "title": title,
            "body": " ".join(piece for piece in pieces if piece).strip(),
            "href": (
                f"/app/tenders/{row.tender_id}?tab=checklist"
                if row.tender_id
                else "/app/checklist"
            ),
            "due_at": row.due_at,
            "lead_days": row.lead_days,
            "state": row.state,
            "sent_at": row.sent_at,
            "acknowledged_at": row.acknowledged_at,
            "is_acknowledged": row.acknowledged_at is not None,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "created_at": row.created_at,
        }

    def get(self, company: Company, reminder_id: uuid.UUID) -> Reminder:
        row = self.reminders.get(company.id, reminder_id)
        if row is None:
            raise NotFound("Reminder not found.")
        return row

    # -- human actions ----------------------------------------------------

    def acknowledge(
        self, company: Company, reminder_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> dict[str, Any]:
        row = self.get(company, reminder_id)
        before = {"state": row.state, "acknowledged_at": row.acknowledged_at}
        try:
            self.reminders.mark_acknowledged(row, now=utcnow())
            self.audit.log(
                company_id=company.id,
                actor_user_id=actor_user_id,
                action="reminder.acknowledged",
                entity_type="reminder",
                entity_id=row.id,
                before=before,
                after={
                    "state": row.state,
                    "acknowledged_at": row.acknowledged_at.isoformat()
                    if row.acknowledged_at
                    else None,
                },
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return self._out(row)

    def dismiss(
        self, company: Company, reminder_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> dict[str, Any]:
        row = self.get(company, reminder_id)
        before = {"state": row.state}
        try:
            self.reminders.dismiss(row, now=utcnow())
            self.audit.log(
                company_id=company.id,
                actor_user_id=actor_user_id,
                action="reminder.dismissed",
                entity_type="reminder",
                entity_id=row.id,
                before=before,
                after={"state": row.state},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return self._out(row)

    # -- email ------------------------------------------------------------

    def send_due(
        self,
        *,
        dry_run: bool = True,
        now: datetime | None = None,
        limit: int = 100,
        email: EmailService | None = None,
    ) -> dict[str, Any]:
        """Send due reminders for opted-in companies.

        The default is a dry run: it reports what would go out and sends
        nothing. Live sending requires ``SMTP_APP_PASSWORD``.
        """

        now = now or utcnow()
        mailer = email or self.email
        companies = list(
            self.session.scalars(
                select(Company)
                .where(Company.is_active.is_(True))
                .order_by(Company.created_at.asc())
            ).all()
        )
        sent: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        if not dry_run and not mailer.configured:
            from app.core.errors import ServiceUnavailable

            raise ServiceUnavailable(
                "Reminder email is not configured. Set SMTP_USERNAME and "
                "SMTP_APP_PASSWORD."
            )

        for company in companies:
            self.sync(company, now=now)
            if not company.reminder_opt_in:
                skipped.append(
                    {"company_id": str(company.id), "reason": "email opt-in is off"}
                )
                continue
            user = self.session.scalar(
                select(User)
                .where(
                    User.company_id == company.id,
                    User.is_active.is_(True),
                    User.email_verified.is_(True),
                )
                .order_by(User.created_at.asc())
                .limit(1)
            )
            if user is None:
                skipped.append(
                    {"company_id": str(company.id), "reason": "no verified user"}
                )
                continue

            if dry_run:
                remaining = max(limit - len(sent), 0)
                for row in self.reminders.due_for_company(
                    company.id, now, limit=remaining
                ):
                    output = self._out(row)
                    subject = f"BidKaro reminder: {output['title']}"
                    sent.append(
                        {
                            "company_id": str(company.id),
                            "reminder_id": str(row.id),
                            "to": user.email,
                            "subject": subject,
                            "due_at": row.due_at.isoformat(),
                        }
                    )
                if len(sent) >= limit:
                    break
                continue

            while len(sent) + len(failed) < limit:
                row = self.reminders.claim_due(company.id, now)
                if row is None:
                    break
                output = self._out(row)
                subject = f"BidKaro reminder: {output['title']}"
                body = "\n".join(
                    [
                        output["title"],
                        "",
                        output["body"],
                        "",
                        "Open BidKaro to review this reminder.",
                        "",
                        "BidKaro",
                    ]
                )
                try:
                    mailer.send_reminder(to=user.email, subject=subject, body=body)
                    self.session.commit()
                    sent.append(
                        {
                            "company_id": str(company.id),
                            "reminder_id": str(row.id),
                            "to": user.email,
                            "subject": subject,
                            "due_at": row.due_at.isoformat(),
                        }
                    )
                except Exception as exc:
                    self.reminders.mark_failed(row, error=str(exc), now=now)
                    self.session.commit()
                    failed.append(
                        {
                            "company_id": str(company.id),
                            "reminder_id": str(row.id),
                            "error": str(exc),
                        }
                    )
                    # Leave the row open for the next scheduled run, but do
                    # not retry the same failure in a tight loop.
                    break
                if len(sent) + len(failed) >= limit:
                    break

        return {
            "dry_run": dry_run,
            "checked_at": now.isoformat(),
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
        }
