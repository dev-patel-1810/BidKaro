from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.time import utcnow
from app.models.assessment import ChecklistTask
from app.models.corpus import Tender
from app.models.identity import Company
from app.repositories.assessment import ChecklistRepository, CompanyTenderRepository
from app.repositories.corpus import DocumentRepository, TenderRepository


def _escape(value: str | None) -> str:
    text = (value or "").replace("\\", "\\\\")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(";", "\\;").replace(",", "\\,")
    return text.replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets, preserving UTF-8 characters."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks: list[bytes] = []
    current = b""
    for char in line:
        piece = char.encode("utf-8")
        if len(current) + len(piece) > 73:
            chunks.append(current)
            current = b" " + piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return "\r\n".join(chunk.decode("utf-8") for chunk in chunks)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_date_only(value: datetime) -> bool:
    utc = _utc(value)
    return utc.time() == time(12, 0)


def _date_line(prefix: str, value: datetime) -> str:
    utc = _utc(value)
    if _is_date_only(value):
        return f"{prefix};VALUE=DATE:{utc.strftime('%Y%m%d')}"
    return f"{prefix}:{utc.strftime('%Y%m%dT%H%M%SZ')}"


def _event(
    *,
    uid: str,
    summary: str,
    due_at: datetime,
    description: str,
    url: str | None = None,
) -> list[str]:
    stamp = _utc(utcnow()).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_escape(uid)}",
        f"DTSTAMP:{stamp}",
        _date_line("DTSTART", due_at),
    ]
    if _is_date_only(due_at):
        lines.append(_date_line("DTEND", due_at + timedelta(days=1)))
    else:
        lines.append(_date_line("DTEND", due_at + timedelta(hours=1)))
    lines.extend(
        [
            f"SUMMARY:{_escape(summary)}",
            f"DESCRIPTION:{_escape(description)}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
        ]
    )
    if url:
        lines.append(f"URL:{_escape(url)}")
    lines.extend(
        [
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(summary)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    )
    return lines


def _wrap(name: str, events: list[list[str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//BidKaro//Tender calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
        "X-WR-TIMEZONE:UTC",
    ]
    for event in events:
        lines.extend(event)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


class CalendarService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.tenders = TenderRepository(session)
        self.documents = DocumentRepository(session)
        self.company_tenders = CompanyTenderRepository(session)
        self.tasks = ChecklistRepository(session)

    def _tender_events(self, tender: Tender) -> list[list[str]]:
        if tender.closing_at is None:
            return []
        description = (
            f"Tender: {tender.work_title or 'Untitled tender'}\n"
            f"Authority: {tender.authority or 'not stated'}\n"
            f"Reference: {tender.reference or 'not stated'}\n"
            f"Source: {tender.source_url or 'not recorded'}\n"
            f"Source retrieved: "
            f"{tender.retrieved_at.isoformat() if tender.retrieved_at else 'not recorded'}\n"
            "Deadline from the recorded source. Confirm details on the official portal."
        )
        return [
            _event(
                uid=f"tender-{tender.id}-deadline@bidkaro",
                summary=f"Tender deadline: {tender.work_title or 'Untitled tender'}",
                due_at=tender.closing_at,
                description=description,
                url=tender.source_url,
            )
        ]

    def _task_events(self, task: ChecklistTask) -> list[list[str]]:
        if task.due_at is None:
            return []
        citation: list[str] = []
        if task.document_id is not None:
            document = self.documents.get(task.document_id)
            if document is not None:
                citation.append(f"Document: {document.filename}")
                if task.page_index is not None:
                    citation.append(f"Page: {task.page_index}")
                if document.source_url:
                    citation.append(f"Source: {document.source_url}")
                if document.fetched_at:
                    citation.append(
                        f"Source retrieved: {document.fetched_at.isoformat()}"
                    )
        if task.clause_ref:
            citation.append(f"Clause: {task.clause_ref}")
        if task.source_url and not any(
            line.startswith("Source:") for line in citation
        ):
            citation.append(f"Source: {task.source_url}")
        if task.excerpt:
            citation.append(f"Source excerpt: {task.excerpt}")
        description = (
            f"Task: {task.title}\n"
            f"Phase: {task.phase}\n"
            f"Status: {task.status}\n"
            f"Acceptance: {task.acceptance_criteria or 'not recorded'}\n"
            f"Evidence check: {task.evidence_check or 'not recorded'}\n"
            + ("\n".join(citation) or "Citation: not recorded")
        )
        return [
            _event(
                uid=f"task-{task.id}@bidkaro",
                summary=f"Bid task: {task.title}",
                due_at=task.due_at,
                description=description,
                url=task.source_url,
            )
        ]

    def tender_calendar(self, *, company: Company, tender_id: uuid.UUID) -> str:
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        tasks = self.tasks.list_for_tender(company.id, tender.id)
        events = self._tender_events(tender)
        for task in tasks:
            events.extend(self._task_events(task))
        return _wrap(
            f"BidKaro - {tender.work_title or 'Tender'}",
            events,
        )

    def company_calendar(self, *, company: Company) -> str:
        events: list[list[str]] = []
        for row in self.company_tenders.list_active(company.id):
            tender = self.tenders.get(row.tender_id)
            if tender is not None:
                events.extend(self._tender_events(tender))
        for task in self.tasks.list_for_company(company.id):
            if task.status in {"done", "not_applicable"}:
                continue
            events.extend(self._task_events(task))
        return _wrap("BidKaro deadlines and tasks", events)
