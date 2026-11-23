from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context
from app.core.db import get_db
from app.schemas.corpus import (
    RequirementFlagRequest,
    RequirementOut,
    RequirementPatchRequest,
)
from app.services.requirement_service import RequirementService

router = APIRouter(prefix="/requirements", tags=["requirements"])


@router.get("/{requirement_id}", response_model=RequirementOut)
def get_requirement(
    requirement_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementOut:
    return RequirementOut.model_validate(
        RequirementService(session).get_out(
            requirement_id, company_id=context.company.id
        )
    )


@router.patch("/{requirement_id}", response_model=RequirementOut)
def review_requirement(
    requirement_id: uuid.UUID,
    payload: RequirementPatchRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementOut:
    return RequirementOut.model_validate(
        RequirementService(session).review(
            requirement_id=requirement_id,
            state=payload.state,
            note=payload.note,
            company_id=context.company.id,
            actor_user_id=context.user.id,
        )
    )


@router.post("/{requirement_id}/flag", response_model=RequirementOut)
def flag_requirement(
    requirement_id: uuid.UUID,
    payload: RequirementFlagRequest | None = None,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementOut:
    ignored = payload.ignored if payload is not None else True
    set_note = payload is not None and "note" in payload.model_fields_set
    return RequirementOut.model_validate(
        RequirementService(session).flag(
            requirement_id=requirement_id,
            company_id=context.company.id,
            actor_user_id=context.user.id,
            ignored=True if ignored is None else ignored,
            note=payload.note if payload is not None else None,
            set_note=set_note,
        )
    )


@router.patch("/{requirement_id}/flag", response_model=RequirementOut)
def update_flag(
    requirement_id: uuid.UUID,
    payload: RequirementFlagRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementOut:
    return RequirementOut.model_validate(
        RequirementService(session).flag(
            requirement_id=requirement_id,
            company_id=context.company.id,
            actor_user_id=context.user.id,
            ignored=payload.ignored,
            note=payload.note,
            set_note="note" in payload.model_fields_set,
        )
    )
