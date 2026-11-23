from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context
from app.core.db import get_db
from app.core.errors import BadRequest
from app.repositories.identity import AuditRepository
from app.schemas.auth import CompanyOut
from app.schemas.company import (
    CapabilityOut,
    CapabilityPatch,
    CompanyBriefSaveRequest,
    CompanyDocumentOut,
    CompanyDocumentPatch,
    CompanyUnderstandingOut,
    CompanyUpdateRequest,
    ExperienceOut,
    ExperiencePatch,
)
from app.services.company_brief import CompanyBriefService, payload_terms
from app.services.company_intel import ABOUT_COMPANY_TYPE, CompanyDocumentIntelService
from app.services.company_service import CompanyService
from app.services.company_understanding import CompanyUnderstandingService
from app.services.jobs import JobService

router = APIRouter(prefix="/company", tags=["company"])


@router.get("", response_model=CompanyOut)
def get_company(context: AuthContext = Depends(get_auth_context)) -> CompanyOut:
    return CompanyOut.model_validate(context.company)


@router.put("", response_model=CompanyOut)
def replace_company(
    payload: CompanyUpdateRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> CompanyOut:
    return CompanyService(session).update(context.company, payload)


@router.patch("", response_model=CompanyOut)
def patch_company(
    payload: CompanyUpdateRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> CompanyOut:
    return CompanyService(session).update(context.company, payload)


@router.get("/brief")
def get_brief(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    """The current editable brief, or a deterministic preview before one exists.

    The four role groups are returned beside the stored row so the UI can show
    ``technical support`` as support and ``document scanning`` as core without
    duplicating the role rules in JavaScript.
    """

    return CompanyBriefService(session).current_or_preview(context.company)


@router.post("/brief/generate")
def generate_brief(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    """Create a new brief version from the profile and confirmed understanding.

    This is deterministic: the AI has already read the PDF into capabilities and
    experiences, and this step groups those words into roles. It never deletes a
    human-edited version; it writes the next version and makes it current.
    """

    service = CompanyBriefService(session)
    row = service.generate(
        context.company,
        actor_user_id=context.user.id,
        source_kind="generated",
    )
    AuditRepository(session).log(
        company_id=context.company.id,
        actor_user_id=context.user.id,
        action="company.brief_generated",
        entity_type="company_brief",
        entity_id=row.id,
        after={"version": row.version},
    )
    session.commit()
    return {
        "brief": service.out(row),
        "terms": payload_terms(row.payload),
        "rules_version": row.prompt_version,
    }


@router.post("/brief")
def save_brief(
    payload: CompanyBriefSaveRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    """Save an edited brief as a new version.

    The edit is additive history: a judgment that used version 3 still names
    version 3's words. The current row is the one discovery reads.
    """

    service = CompanyBriefService(session)
    row = service.save(
        context.company,
        payload=payload.payload.model_dump(exclude_none=False),
        actor_user_id=context.user.id,
    )
    AuditRepository(session).log(
        company_id=context.company.id,
        actor_user_id=context.user.id,
        action="company.brief_saved",
        entity_type="company_brief",
        entity_id=row.id,
        after={"version": row.version},
    )
    session.commit()
    return {
        "brief": service.out(row),
        "terms": payload_terms(row.payload),
        "rules_version": row.prompt_version,
    }


@router.get("/brief/versions")
def list_brief_versions(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    service = CompanyBriefService(session)
    rows = service.repository.versions(context.company.id)
    return {
        "items": [service.version_out(row) for row in rows],
        "next": None,
    }


@router.post("/brief/versions/{brief_id}/activate")
def activate_brief(
    brief_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    service = CompanyBriefService(session)
    row = service.activate(context.company, brief_id=brief_id)
    AuditRepository(session).log(
        company_id=context.company.id,
        actor_user_id=context.user.id,
        action="company.brief_activated",
        entity_type="company_brief",
        entity_id=row.id,
        after={"version": row.version},
    )
    session.commit()
    return {
        "brief": service.out(row),
        "terms": payload_terms(row.payload),
        "rules_version": row.prompt_version,
    }


@router.get("/documents")
def list_documents(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    items = CompanyService(session).list_documents(context.company)
    return {"items": items, "next": None}


@router.post(
    "/documents",
    response_model=CompanyDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    title: str | None = Form(default=None),
    issuer: str | None = Form(default=None),
    signatory: str | None = Form(default=None),
    issued_on: date | None = Form(default=None),
    expires_on: date | None = Form(default=None),
    financial_year: str | None = Form(default=None),
    project_value_amount: Decimal | None = Form(default=None),
    project_value_currency: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> CompanyDocumentOut:
    data = await file.read()
    document = CompanyService(session).upload_document(
        company=context.company,
        data=data,
        filename=file.filename or "upload",
        document_type=document_type,
        title=title,
        issuer=issuer,
        signatory=signatory,
        issued_on=issued_on,
        expires_on=expires_on,
        financial_year=financial_year,
        project_value_amount=project_value_amount,
        project_value_currency=project_value_currency,
        notes=notes,
    )
    # An about-company profile changes what discovery suggests, so it is read
    # as soon as it is stored. Every other type is evidence and is not read for
    # suggestions. The key makes a duplicate upload one job, not two.
    if document.document_type == ABOUT_COMPANY_TYPE and document.mime == "application/pdf":
        JobService(session).enqueue(
            kind="extract_company_document",
            company_id=context.company.id,
            priority=60,
            payload={
                "document_id": str(document.id),
                "company_id": str(context.company.id),
            },
            idempotency_key=f"company-doc-intel:{document.id}",
        )
    return document


@router.post(
    "/documents/{document_id}/extract",
    status_code=status.HTTP_202_ACCEPTED,
)
def extract_document(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    """Read an about-company profile again, for example after a failed OCR."""

    document = CompanyService(session).get_document(context.company, document_id)
    if document.document_type != ABOUT_COMPANY_TYPE:
        raise BadRequest(
            "Only an about-company profile is read for suggestions."
        )
    latest = max(
        document.versions,
        key=lambda version: (version.uploaded_at, version.created_at),
        default=None,
    )
    if latest is None or latest.mime != "application/pdf":
        raise BadRequest("Upload a PDF company profile so its text can be read.")
    # The read is asynchronous; saying "pending" now is honest until the job
    # writes a terminal state, instead of leaving the old "done" visible.
    document.extraction_state = "pending"
    document.extraction_error = None
    session.commit()
    job, _ = JobService(session).enqueue(
        kind="extract_company_document",
        company_id=context.company.id,
        priority=60,
        payload={
            "document_id": str(document.id),
            "company_id": str(context.company.id),
        },
    )
    return {"job_id": job.id, "document_id": document.id}


@router.get("/documents/{document_id}/terms")
def list_document_terms(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    document = CompanyService(session).get_document(context.company, document_id)
    return CompanyDocumentIntelService(session).terms(document)


@router.get("/understanding", response_model=CompanyUnderstandingOut)
def get_understanding(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> CompanyUnderstandingOut:
    """What the company says it does and what it has delivered, with citations.

    Only ``confirmed`` items are used by discovery; the proposed ones are shown
    so a human can confirm or reject them. Nothing here can pass eligibility.
    """

    return CompanyUnderstandingOut.model_validate(
        CompanyUnderstandingService(session).understanding(context.company)
    )


@router.patch("/capabilities/{capability_id}", response_model=CapabilityOut)
def patch_capability(
    capability_id: uuid.UUID,
    payload: CapabilityPatch,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> CapabilityOut:
    return CapabilityOut.model_validate(
        CompanyUnderstandingService(session).update_capability(
            company=context.company,
            capability_id=capability_id,
            payload=payload.model_dump(exclude_unset=True),
            actor_user_id=context.user.id,
        )
    )


@router.patch("/experiences/{experience_id}", response_model=ExperienceOut)
def patch_experience(
    experience_id: uuid.UUID,
    payload: ExperiencePatch,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ExperienceOut:
    return ExperienceOut.model_validate(
        CompanyUnderstandingService(session).update_experience(
            company=context.company,
            experience_id=experience_id,
            payload=payload.model_dump(exclude_unset=True),
            actor_user_id=context.user.id,
        )
    )


@router.delete(
    "/documents/{document_id}/terms/{term_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_document_term(
    document_id: uuid.UUID,
    term_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> None:
    document = CompanyService(session).get_document(context.company, document_id)
    CompanyDocumentIntelService(session).remove_term(
        document=document, term_id=term_id
    )


@router.patch("/documents/{document_id}", response_model=CompanyDocumentOut)
def patch_document(
    document_id: uuid.UUID,
    payload: CompanyDocumentPatch,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> CompanyDocumentOut:
    document = CompanyService(session).patch_document(
        company=context.company, document_id=document_id, payload=payload
    )
    # Changing a stored PDF into an about-company profile is a request to read
    # it, exactly like uploading one. A profile already read is left alone.
    if (
        document.document_type == ABOUT_COMPANY_TYPE
        and document.mime == "application/pdf"
        and document.extraction_state != "done"
    ):
        JobService(session).enqueue(
            kind="extract_company_document",
            company_id=context.company.id,
            priority=60,
            payload={
                "document_id": str(document.id),
                "company_id": str(context.company.id),
            },
        )
    return document


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> None:
    CompanyService(session).soft_delete_document(context.company, document_id)


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> FileResponse:
    path, mime, filename = CompanyService(session).download_document(
        context.company, document_id
    )
    return FileResponse(path, media_type=mime, filename=filename)

