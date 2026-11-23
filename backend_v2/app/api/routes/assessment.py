from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context
from app.core.db import get_db
from app.schemas.assessment import (
    AssessmentOut,
    ChecklistGenerateOut,
    ChecklistTaskCreate,
    ChecklistTaskListOut,
    ChecklistTaskOut,
    ChecklistTaskUpdate,
    EvidenceDecisionRequest,
    LegalObligationListOut,
    RequirementEvidenceOut,
)
from app.services.assessment import AssessmentService

router = APIRouter(tags=["assessment"])

LEGAL_DISCLAIMER = (
    "Extracted obligations with source citations only. This is not legal advice "
    "and does not replace review by a qualified professional."
)


def _assess(
    *,
    tender_id: uuid.UUID,
    context: AuthContext,
    session: Session,
) -> AssessmentOut:
    return AssessmentOut.model_validate(
        AssessmentService(session).assess(
            tender_id=tender_id, company=context.company
        )
    )


@router.get("/tenders/{tender_id}/assessment", response_model=AssessmentOut)
def get_assessment(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> AssessmentOut:
    return _assess(tender_id=tender_id, context=context, session=session)


@router.post("/tenders/{tender_id}/assessment", response_model=AssessmentOut)
def recompute_assessment(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> AssessmentOut:
    return _assess(tender_id=tender_id, context=context, session=session)


@router.get(
    "/tenders/{tender_id}/requirements/{requirement_id}/evidence",
    response_model=RequirementEvidenceOut,
)
def get_requirement_evidence(
    tender_id: uuid.UUID,
    requirement_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementEvidenceOut:
    return RequirementEvidenceOut.model_validate(
        AssessmentService(session).evidence_for_requirement(
            tender_id=tender_id,
            requirement_id=requirement_id,
            company=context.company,
        )
    )


@router.post(
    "/tenders/{tender_id}/requirements/{requirement_id}/evidence",
    response_model=RequirementEvidenceOut,
)
def decide_requirement_evidence(
    tender_id: uuid.UUID,
    requirement_id: uuid.UUID,
    payload: EvidenceDecisionRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementEvidenceOut:
    return RequirementEvidenceOut.model_validate(
        AssessmentService(session).decide_evidence(
            tender_id=tender_id,
            requirement_id=requirement_id,
            company=context.company,
            user_id=context.user.id,
            document_id=payload.document_id,
            document_version_id=payload.document_version_id,
            status=payload.status,
            note=payload.note,
        )
    )


@router.get(
    "/tenders/{tender_id}/legal", response_model=LegalObligationListOut
)
def list_legal_obligations(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> LegalObligationListOut:
    items = AssessmentService(session).list_legal_obligations(
        tender_id=tender_id, company_id=context.company.id
    )
    return LegalObligationListOut(
        items=items, total=len(items), disclaimer=LEGAL_DISCLAIMER
    )


@router.get("/checklist", response_model=ChecklistTaskListOut)
def list_company_checklist(
    phase: str | None = Query(default=None, pattern="^(pre_award|post_award)$"),
    status_filter: list[str] | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=2000),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChecklistTaskListOut:
    items = AssessmentService(session).list_company_tasks(
        company_id=context.company.id,
        phase=phase,
        status=status_filter,
        limit=limit,
    )
    return ChecklistTaskListOut(
        items=[ChecklistTaskOut.model_validate(item) for item in items],
        total=len(items),
    )


@router.get(
    "/tenders/{tender_id}/checklist", response_model=ChecklistTaskListOut
)
def list_tender_checklist(
    tender_id: uuid.UUID,
    phase: str | None = Query(default=None, pattern="^(pre_award|post_award)$"),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChecklistTaskListOut:
    items = AssessmentService(session).list_tasks(
        tender_id=tender_id, company_id=context.company.id, phase=phase
    )
    return ChecklistTaskListOut(
        items=[ChecklistTaskOut.model_validate(item) for item in items],
        total=len(items),
    )


@router.post(
    "/tenders/{tender_id}/checklist/generate",
    response_model=ChecklistGenerateOut,
)
def generate_tender_checklist(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChecklistGenerateOut:
    created, skipped, total = AssessmentService(session).generate_checklist(
        tender_id=tender_id, company=context.company, user_id=context.user.id
    )
    return ChecklistGenerateOut(
        created=len(created),
        skipped=skipped,
        total=total,
        items=[ChecklistTaskOut.model_validate(item) for item in created],
        message=(
            f"Created {len(created)} tender-specific checklist items"
            + (f"; {skipped} already existed." if skipped else ".")
        ),
    )


@router.post(
    "/checklist",
    response_model=ChecklistTaskOut,
    status_code=status.HTTP_201_CREATED,
)
def create_checklist_task(
    payload: ChecklistTaskCreate,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChecklistTaskOut:
    task = AssessmentService(session).create_task(
        company=context.company,
        user_id=context.user.id,
        payload=payload.model_dump(exclude_unset=True),
    )
    return ChecklistTaskOut.model_validate(task)


@router.patch("/checklist/{task_id}", response_model=ChecklistTaskOut)
def update_checklist_task(
    task_id: uuid.UUID,
    payload: ChecklistTaskUpdate,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChecklistTaskOut:
    task = AssessmentService(session).update_task(
        task_id=task_id,
        company_id=context.company.id,
        user_id=context.user.id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return ChecklistTaskOut.model_validate(task)
