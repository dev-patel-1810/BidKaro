from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context
from app.core.db import get_db
from app.schemas.corpus import TenderOut
from app.schemas.workflow import (
    ChangeListOut,
    ChangeOut,
    ChangeReviewRequest,
    ReminderListOut,
    ReminderOut,
    SnapshotAccepted,
    SnapshotListOut,
    SnapshotOut,
)
from app.services.calendar import CalendarService
from app.services.jobs import JobService
from app.services.reminders import ReminderService
from app.services.tender_service import TenderService
from app.services.workflow import WorkflowService

router = APIRouter(tags=["workflow"])


def _snapshot_out(snapshot) -> SnapshotOut:
    return SnapshotOut.model_validate(snapshot).model_copy(
        update={
            "requirement_count": len(snapshot.requirement_fingerprints or []),
        }
    )


def _filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return (cleaned or "bidkaro")[:80]


@router.get("/tenders/{tender_id}/snapshots", response_model=SnapshotListOut)
def list_snapshots(
    tender_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> SnapshotListOut:
    snapshots, total = WorkflowService(session).list_snapshots(
        tender_id, limit=limit, offset=offset
    )
    return SnapshotListOut(
        items=[_snapshot_out(snapshot) for snapshot in snapshots],
        total=total,
        next=None,
    )


@router.post(
    "/tenders/{tender_id}/snapshot",
    response_model=SnapshotAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def capture_snapshot(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> SnapshotAccepted:
    # The tender is global corpus; reading it here also gives the caller the
    # usual 404 for an unknown id before a job is queued.
    TenderService(session).get(tender_id)
    job, _ = JobService(session).enqueue(
        kind="snapshot_tender",
        company_id=context.company.id,
        priority=60,
        payload={
            "tender_id": str(tender_id),
            "company_id": str(context.company.id),
            "user_id": str(context.user.id),
        },
    )
    return SnapshotAccepted(job_id=job.id, state=job.state, tender_id=tender_id)


@router.get("/tenders/{tender_id}/changes", response_model=ChangeListOut)
def list_tender_changes(
    tender_id: uuid.UUID,
    review_state: str | None = Query(
        default=None, pattern="^(pending|acknowledged|dismissed)$"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChangeListOut:
    changes, total = WorkflowService(session).list_changes(
        tender_id, review_state=review_state, limit=limit, offset=offset
    )
    return ChangeListOut(
        items=[ChangeOut.model_validate(change) for change in changes],
        total=total,
        next=None,
    )


@router.get("/changes", response_model=ChangeListOut)
def list_company_changes(
    review_state: str | None = Query(
        default=None, pattern="^(pending|acknowledged|dismissed)$"
    ),
    saved_only: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChangeListOut:
    changes, total = WorkflowService(session).list_company_changes(
        context.company.id,
        saved_only=saved_only,
        review_state=review_state,
        limit=limit,
        offset=offset,
    )
    return ChangeListOut(
        items=[ChangeOut.model_validate(change) for change in changes],
        total=total,
        next=None,
    )


@router.patch("/changes/{change_id}", response_model=ChangeOut)
def review_change(
    change_id: uuid.UUID,
    payload: ChangeReviewRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChangeOut:
    change = WorkflowService(session).review_change(
        change_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
        review_state=payload.review_state,
    )
    return ChangeOut.model_validate(change)


@router.get("/tenders/{tender_id}/reminders", response_model=ReminderListOut)
def list_tender_reminders(
    tender_id: uuid.UUID,
    state: str | None = Query(default=None, pattern="^(open|sent|dismissed)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ReminderListOut:
    TenderService(session).get(tender_id)
    service = ReminderService(session)
    items, total = service.list(
        context.company,
        state=state,
        tender_id=tender_id,
        limit=limit,
        offset=offset,
    )
    return ReminderListOut(
        items=[ReminderOut.model_validate(item) for item in items],
        total=total,
        next=None,
        email_enabled=context.company.reminder_opt_in,
        email_configured=service.email.configured,
    )


@router.get("/reminders", response_model=ReminderListOut)
def list_reminders(
    state: str | None = Query(default=None, pattern="^(open|sent|dismissed)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ReminderListOut:
    service = ReminderService(session)
    items, total = service.list(
        context.company, state=state, limit=limit, offset=offset
    )
    return ReminderListOut(
        items=[ReminderOut.model_validate(item) for item in items],
        total=total,
        next=None,
        email_enabled=context.company.reminder_opt_in,
        email_configured=service.email.configured,
    )


@router.post("/reminders/{reminder_id}/acknowledge", response_model=ReminderOut)
def acknowledge_reminder(
    reminder_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ReminderOut:
    return ReminderOut.model_validate(
        ReminderService(session).acknowledge(
            context.company, reminder_id, actor_user_id=context.user.id
        )
    )


@router.post("/reminders/{reminder_id}/dismiss", response_model=ReminderOut)
def dismiss_reminder(
    reminder_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ReminderOut:
    return ReminderOut.model_validate(
        ReminderService(session).dismiss(
            context.company, reminder_id, actor_user_id=context.user.id
        )
    )


@router.get("/calendar/tender/{tender_id}.ics")
def tender_calendar(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> Response:
    tender = TenderService(session).get(tender_id)
    body = CalendarService(session).tender_calendar(
        company=context.company, tender_id=tender_id
    )
    filename = _filename(f"bidkaro-tender-{tender.work_title or tender_id}") + ".ics"
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/calendar/tasks.ics")
def tasks_calendar(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> Response:
    body = CalendarService(session).company_calendar(company=context.company)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="bidkaro-tasks.ics"'
        },
    )


@router.post("/tenders/{tender_id}/lineage/propose", response_model=TenderOut)
def propose_lineage(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    service.get(tender_id)
    WorkflowService(session).propose_lineage(
        tender_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    return TenderOut.model_validate(service.out(service.get(tender_id)))


@router.post(
    "/tenders/{tender_id}/lineage/{candidate_id}/confirm", response_model=TenderOut
)
def confirm_lineage(
    tender_id: uuid.UUID,
    candidate_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    WorkflowService(session).confirm_lineage(
        tender_id,
        candidate_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    return TenderOut.model_validate(service.out(service.get(tender_id)))


@router.post(
    "/tenders/{tender_id}/lineage/{candidate_id}/reject", response_model=TenderOut
)
def reject_lineage(
    tender_id: uuid.UUID,
    candidate_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    WorkflowService(session).reject_lineage(
        tender_id,
        candidate_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    return TenderOut.model_validate(service.out(service.get(tender_id)))
