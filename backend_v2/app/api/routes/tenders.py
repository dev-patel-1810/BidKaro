from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_ai_provider, get_auth_context
from app.core.db import get_db
from app.schemas.corpus import (
    CoverOut,
    DocumentArchiveOut,
    DocumentListOut,
    DocumentOut,
    FetchStateOut,
    RequirementCreateRequest,
    RequirementListOut,
    RequirementOut,
    RequirementSummaryOut,
    TenderListOut,
    TenderOut,
)
from app.services.document_service import DocumentService
from app.services.requirement_experience import RequirementExperienceService
from app.services.requirement_service import RequirementService
from app.services.similarity import TenderSimilarityService
from app.services.tender_service import TenderService

router = APIRouter(prefix="/tenders", tags=["tenders"])


def _document_out(service: DocumentService, document) -> DocumentOut:
    return DocumentOut.model_validate(document).model_copy(
        update={"citation_counts": service.citation_counts(document.id)}
    )


def _relevance(service: TenderService, company, tender) -> dict:
    """The read-time signal the card shows.

    The verdict is the same ladder the listing gate used, so a card that says
    "Strong match" is exactly the card the gate let through on that basis. The
    route does not compute a second, differently scaled score.
    """

    verdict = service.last_verdicts.get(tender.id) or service.verdict_for(
        company, tender
    )
    score = verdict.get("score", 0.0)
    reasons = verdict.get("reasons", [])
    return {
        "score": score,
        "match_score": score,
        "tier": verdict.get("tier", 7),
        "bucket": verdict.get("bucket", "possible"),
        "matched_terms": verdict.get("matched_terms", []),
        "reasons": reasons,
        "match_reasons": reasons,
        "region_match": verdict.get("region_match", False),
        "profile_complete": verdict.get("profile_complete", True),
        "source": verdict.get("source", "rules"),
    }


@router.get("", response_model=TenderListOut)
def list_tenders(
    closing_after: datetime | None = None,
    saved: bool = False,
    bidding: bool = False,
    mine: bool = False,
    include_dismissed: bool = False,
    include_hidden: bool = False,
    include_closed: bool = False,
    q: str | None = Query(default=None, max_length=200),
    sort: Literal[
        "created_desc", "created_asc", "closing_asc", "closing_desc", "match_desc"
    ] = "created_desc",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderListOut:
    service = TenderService(session)
    tenders, total = service.list(
        company=context.company,
        closing_after=closing_after,
        saved=saved,
        bidding=bidding,
        mine=mine,
        include_dismissed=include_dismissed,
        include_hidden=include_hidden,
        include_closed=include_closed,
        query=q,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    pages = max(1, (total + limit - 1) // limit)
    return TenderListOut(
        items=[
            TenderOut.model_validate(
                service.out(
                    tender,
                    relevance=_relevance(service, context.company, tender),
                    company=context.company,
                )
            )
            for tender in tenders
        ],
        total=total,
        page=offset // limit + 1,
        pages=pages,
        next=str(offset + limit) if offset + limit < total else None,
        profile_complete=service.profile_complete(context.company),
    )


@router.post("", response_model=TenderOut, status_code=status.HTTP_201_CREATED)
async def create_manual_tender(
    response: Response,
    file: UploadFile = File(...),
    work_title: str = Form(...),
    authority: str | None = Form(default=None),
    reference: str | None = Form(default=None),
    location: str | None = Form(default=None),
    region: str | None = Form(default=None),
    work_type: str | None = Form(default=None),
    closing_at: datetime | None = Form(default=None),
    published_at: datetime | None = Form(default=None),
    value_amount: Decimal | None = Form(default=None),
    source_url: str | None = Form(default=None),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    """Manual upload fallback. There is no discovery module in the base yet."""

    data = await file.read()
    tender, created = TenderService(session).create_manual(
        work_title=work_title,
        data=data,
        filename=file.filename,
        authority=authority,
        reference=reference,
        location=location,
        region=region,
        work_type=work_type,
        closing_at=closing_at,
        published_at=published_at,
        value_amount=value_amount,
        source_url=source_url,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return TenderOut.model_validate(TenderService(session).out(tender))


@router.get("/{tender_id}", response_model=TenderOut)
def get_tender(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    tender = service.get(tender_id)
    # One row lookup so the detail response says truthfully whether this
    # company has the tender saved, instead of defaulting to "no".
    service.last_links[tender.id] = service.company_tenders.get(
        context.company.id, tender.id
    )
    return TenderOut.model_validate(
        service.out(
            tender,
            relevance=_relevance(service, context.company, tender),
            company=context.company,
        )
    )


@router.get("/{tender_id}/similarity")
def get_tender_similarity(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    """The cached closed-label answer, or null when the tender was not judged."""

    service = TenderService(session)
    tender = service.get(tender_id)
    return {
        "tender_id": tender.id,
        "similarity": service.similarity_for(context.company, tender),
    }


@router.post("/{tender_id}/similarity")
def judge_tender_similarity(
    tender_id: uuid.UUID,
    force: bool = False,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
    ai=Depends(get_ai_provider),
) -> dict:
    """Ask the closed-label judge about one tender, on demand.

    Unlike the batch action this answers in the request, bounded to one or a
    few model calls. It only says whether the notice is the company's kind of
    work; eligibility is never involved.
    """

    service = TenderService(session)
    tender = service.get(tender_id)
    similarity = TenderSimilarityService(session, ai=ai).judge_one(
        company=context.company, tender=tender, force=force
    )
    return {"tender_id": tender.id, "similarity": similarity}


@router.post("/{tender_id}/save", response_model=TenderOut)
def save_tender(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    tender = service.save(
        tender_id=tender_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
        company=context.company,
    )
    return TenderOut.model_validate(service.out(tender, company=context.company))


@router.delete("/{tender_id}/save", response_model=TenderOut)
def unsave_tender(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    """Remove a tender from My tenders without calling it irrelevant.

    The tender is not dismissed, so the gate decides about it again on
    Discover and it can be saved once more. A bid already in preparation keeps
    the row in My tenders (`bidding: true` in the response); "not relevant"
    is the action that removes it from both lists and records the negative
    weight.
    """

    service = TenderService(session)
    tender = service.unsave(
        tender_id=tender_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    return TenderOut.model_validate(service.out(tender, company=context.company))


@router.post("/{tender_id}/not-relevant", response_model=TenderOut)
def mark_not_relevant(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    """Hide a tender and remember the negative signal.

    This is deliberately not a delete and not a ban: the tender is hidden from
    the list like a dismissal, and the work it describes is demoted for future
    matches. The company's own words keep matching everything else.
    """

    service = TenderService(session)
    tender = service.mark_not_relevant(
        tender_id=tender_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
        company=context.company,
    )
    return TenderOut.model_validate(service.out(tender, company=context.company))


@router.post("/{tender_id}/dismiss", response_model=TenderOut)
def dismiss_tender(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    tender = service.dismiss(
        tender_id=tender_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    return TenderOut.model_validate(service.out(tender, company=context.company))


@router.post("/{tender_id}/bidding", response_model=TenderOut)
def start_bidding(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderOut:
    service = TenderService(session)
    tender = service.start_bidding(
        tender_id=tender_id,
        company_id=context.company.id,
        actor_user_id=context.user.id,
        company=context.company,
    )
    return TenderOut.model_validate(service.out(tender, company=context.company))


@router.get("/{tender_id}/documents", response_model=DocumentListOut)
def list_documents(
    tender_id: uuid.UUID,
    include_versions: bool = False,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DocumentListOut:
    documents = DocumentService(session)
    items = documents.list_for_tender(
        tender_id, include_non_current=include_versions
    )
    state = documents.fetch_state(tender_id)
    covers = documents.covers(tender_id)
    return DocumentListOut(
        items=[_document_out(documents, document) for document in items],
        covers=[CoverOut.model_validate(cover) for cover in covers],
        fetch_state=FetchStateOut.model_validate(state) if state is not None else None,
        next=None,
    )


@router.post(
    "/{tender_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    tender_id: uuid.UUID,
    file: UploadFile = File(...),
    source_url: str | None = Form(default=None),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DocumentOut:
    data = await file.read()
    service = DocumentService(session)
    document = service.upload(
        tender_id=tender_id,
        filename=file.filename,
        data=data,
        source_url=source_url,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    service.sync_fetch_state(tender_id)
    session.commit()
    return _document_out(service, document)


@router.post(
    "/{tender_id}/documents/archive",
    response_model=DocumentArchiveOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document_archive(
    tender_id: uuid.UUID,
    file: UploadFile = File(...),
    source_url: str | None = Form(default=None),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DocumentArchiveOut:
    """Attach every PDF inside an uploaded zip, one tender document each."""

    data = await file.read()
    service = DocumentService(session)
    documents, skipped = service.upload_archive(
        tender_id=tender_id,
        filename=file.filename,
        data=data,
        source_url=source_url,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    service.sync_fetch_state(tender_id)
    session.commit()
    return DocumentArchiveOut(
        documents=[_document_out(service, document) for document in documents],
        skipped=skipped,
    )


@router.get("/{tender_id}/requirements", response_model=RequirementListOut)
def list_requirements(
    tender_id: uuid.UUID,
    state: list[str] | None = Query(default=None),
    category: str | None = None,
    include_navigation: bool = False,
    include_inactive: bool = False,
    include_ignored: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementListOut:
    items, total = RequirementService(session).list_for_tender(
        tender_id,
        company_id=context.company.id,
        state=state,
        category=category,
        include_navigation=include_navigation,
        include_inactive=include_inactive,
        include_ignored=include_ignored,
        limit=limit,
        offset=offset,
    )
    return RequirementListOut(
        items=[RequirementOut.model_validate(item) for item in items],
        total=total,
        next=None,
    )


@router.get(
    "/{tender_id}/requirements/summary", response_model=RequirementSummaryOut
)
def requirement_summary(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementSummaryOut:
    return RequirementSummaryOut.model_validate(
        RequirementService(session).summary(tender_id)
    )


@router.get("/{tender_id}/requirement-experience")
def get_requirement_experience(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    """What the company's own experience shows for each requirement.

    ``covered``/``partial`` carry the company-side page and verbatim line;
    ``new`` means nothing in the confirmed profile shows that work, which is a
    prompt to look rather than a failure. Never an eligibility answer.
    """

    return RequirementExperienceService(session).listing(
        company=context.company, tender_id=tender_id
    )


@router.post("/{tender_id}/requirement-experience")
def map_requirement_experience(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
    ai=Depends(get_ai_provider),
) -> dict:
    """Compare the tender's requirements with confirmed capabilities/experience.

    A bounded, user-triggered call. Unchanged requirements reuse their stored
    mapping, so pressing the button twice does not re-ask the same questions.
    """

    return RequirementExperienceService(session, ai=ai).map_tender(
        company=context.company, tender_id=tender_id
    )


@router.post(
    "/{tender_id}/requirements",
    response_model=RequirementOut,
    status_code=status.HTTP_201_CREATED,
)
def create_requirement(
    tender_id: uuid.UUID,
    payload: RequirementCreateRequest,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> RequirementOut:
    result = RequirementService(session).create_human(
        tender_id=tender_id,
        document_id=payload.document_id,
        page_index=payload.page_index,
        excerpt=payload.excerpt,
        category=payload.category,
        title=payload.title,
        obligation=payload.obligation,
        mandatory_state=payload.mandatory_state,
        phase=payload.phase,
        clause_ref=payload.clause_ref,
        form_ref=payload.form_ref,
        deadline_text=payload.deadline_text,
        consequence_text=payload.consequence_text,
        acceptance_criteria=payload.acceptance_criteria,
        issuer=payload.issuer,
        signatory=payload.signatory,
        evidence_check=payload.evidence_check,
        company_id=context.company.id,
        actor_user_id=context.user.id,
    )
    return RequirementOut.model_validate(result)
