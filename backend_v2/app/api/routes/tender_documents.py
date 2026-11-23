from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_ai_provider, get_auth_context
from app.core.db import get_db
from app.core.errors import BadRequest, ServiceUnavailable
from app.integrations.ai import AIProvider
from app.schemas.corpus import (
    DocumentOut,
    ExtractionAccepted,
    PageIndexOut,
    PageOut,
)
from app.services.document_service import DocumentService
from app.services.jobs import JobService
from app.services.requirement_service import RequirementService

router = APIRouter(prefix="/tender-documents", tags=["tender-documents"])


def _document_out(service: DocumentService, document) -> DocumentOut:
    return DocumentOut.model_validate(document).model_copy(
        update={"citation_counts": service.citation_counts(document.id)}
    )


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DocumentOut:
    service = DocumentService(session)
    return _document_out(service, service.get(document_id))


@router.get("/{document_id}/pages")
def list_pages(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    pages = DocumentService(session).pages(document_id)
    return {
        "items": [PageIndexOut.model_validate(page) for page in pages],
        "next": None,
    }


@router.get("/{document_id}/pages/{page_index}", response_model=PageOut)
def get_page(
    document_id: uuid.UUID,
    page_index: int,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> PageOut:
    if page_index < 1:
        raise BadRequest("Page numbers start at 1.")
    return PageOut.model_validate(
        DocumentService(session).page(document_id, page_index)
    )


@router.get("/{document_id}/download")
def download_document(
    document_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> FileResponse:
    document, path = DocumentService(session).path(document_id)
    return FileResponse(
        path, media_type=document.mime, filename=document.filename
    )


@router.post(
    "/{document_id}/extract",
    response_model=ExtractionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_extraction(
    document_id: uuid.UUID,
    replace: bool = Query(default=False),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
    ai: AIProvider = Depends(get_ai_provider),
) -> ExtractionAccepted:
    documents = DocumentService(session)
    document = documents.get(document_id)
    if not any(
        len(" ".join((page.text or "").split())) >= 40
        for page in documents.pages(document_id)
    ):
        raise BadRequest(
            "This PDF has no extractable text, so there is nothing to send to "
            "the extractor."
        )
    if not ai.configured:
        raise ServiceUnavailable(
            "AI extraction is not configured. Set AI_PROVIDER, the provider "
            "API key and model."
        )

    job, _ = JobService(session).enqueue(
        kind="extract_requirements",
        company_id=context.company.id,
        priority=50,
        payload={
            "document_id": str(document.id),
            "replace": bool(replace),
            "company_id": str(context.company.id),
            "user_id": str(context.user.id),
        },
    )
    return ExtractionAccepted(
        job_id=job.id, state=job.state, document_id=document.id
    )
