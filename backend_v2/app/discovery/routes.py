from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context
from app.api.routes.jobs import _job_out
from app.core.db import get_db
from app.core.errors import BadRequest, NotFound, ServiceUnavailable
from app.core.settings import get_settings
from app.discovery.schemas import (
    DiscoveryRunListOut,
    DiscoveryRunStartOut,
    DiscoverySourceListOut,
    DiscoverySourceOut,
    DiscoveryStatusOut,
    DocumentFetchStartOut,
    SimilarityBatchIn,
    TenderRefreshStartOut,
)
from app.discovery.service import DiscoveryService
from app.discovery.documents import enqueue_fetch
from app.repositories.corpus import TenderRepository
from app.repositories.understanding import CompanyUnderstandingRepository
from app.repositories.work import JobRepository
from app.schemas.jobs import JobOut
from app.services.jobs import JobService
from app.services.tender_service import TenderService

router = APIRouter(prefix="/discovery", tags=["discovery"])
fetch_router = APIRouter(prefix="/tenders", tags=["tenders"])


@router.get("/status", response_model=DiscoveryStatusOut)
def discovery_status(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryStatusOut:
    body = DiscoveryService(session).status()
    return DiscoveryStatusOut.model_validate(body)


@router.get("/sources", response_model=DiscoverySourceListOut)
def discovery_sources(
    limit: int = Query(default=100, ge=1, le=500),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoverySourceListOut:
    items = DiscoveryService(session).list_sources(limit=limit)
    return DiscoverySourceListOut(
        items=[DiscoverySourceOut.model_validate(item) for item in items]
    )


@router.post(
    "/runs",
    response_model=DiscoveryRunStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_discovery_run(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryRunStartOut:
    service = DiscoveryService(session)
    job, created = service.start_run(company=context.company, user=context.user)
    return DiscoveryRunStartOut(
        job=_job_out(job, service.jobs.latest_challenge(job.id)),
        created=created,
    )


@router.get("/runs", response_model=DiscoveryRunListOut)
def list_discovery_runs(
    limit: int = Query(default=50, ge=1, le=200),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryRunListOut:
    service = DiscoveryService(session)
    jobs = service.list_runs(context.company.id, limit=limit)
    return DiscoveryRunListOut(
        items=[
            _job_out(job, service.jobs.latest_challenge(job.id)) for job in jobs
        ]
    )


@router.get("/runs/{run_id}", response_model=JobOut)
def get_discovery_run(
    run_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    service = DiscoveryService(session)
    job = service.get_run(run_id, context.company.id)
    return _job_out(job, service.jobs.latest_challenge(job.id))


@router.post("/runs/{run_id}/cancel", response_model=JobOut)
def cancel_discovery_run(
    run_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    service = DiscoveryService(session)
    job = service.cancel_run(run_id, context.company.id)
    return _job_out(job, service.jobs.latest_challenge(job.id))


@router.post(
    "/sweeps",
    response_model=DiscoveryRunStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_sweep(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryRunStartOut:
    """Queue the whole-portal listing sweep; never blocks the request.

    This is the corpus builder. It reads the directory and every listing page
    and stores what it finds, which is why it does not need a profile. Run
    discovery stays as the profile-first fallback, and the two never run at the
    same time because they share the one browser.
    """

    service = DiscoveryService(session)
    job, created = service.start_sweep(company=context.company, user=context.user)
    return DiscoveryRunStartOut(
        job=_job_out(job, service.jobs.latest_challenge(job.id)),
        created=created,
    )


@router.get("/sweeps", response_model=DiscoveryRunListOut)
def list_sweeps(
    limit: int = Query(default=20, ge=1, le=200),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryRunListOut:
    service = DiscoveryService(session)
    jobs = service.list_sweeps(context.company.id, limit=limit)
    return DiscoveryRunListOut(
        items=[
            _job_out(job, service.jobs.latest_challenge(job.id)) for job in jobs
        ]
    )


@router.get("/sweeps/{sweep_id}", response_model=JobOut)
def get_sweep(
    sweep_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    service = DiscoveryService(session)
    job = service.get_sweep(sweep_id, context.company.id)
    return _job_out(job, service.jobs.latest_challenge(job.id))


@router.post("/sweeps/{sweep_id}/cancel", response_model=JobOut)
def cancel_sweep(
    sweep_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    """Stop a sweep at its next page boundary, keeping what it already read."""

    service = DiscoveryService(session)
    job = service.cancel_sweep(sweep_id, context.company.id)
    return _job_out(job, service.jobs.latest_challenge(job.id))


@router.post(
    "/classify",
    response_model=DiscoveryRunStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_classification(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryRunStartOut:
    """Queue the corpus labelling and verdict pass.

    It is a background chore, not a request-path model call: the list endpoint
    never waits on a provider, and an unconfigured provider only means the
    ambiguous rows stay visible instead of being judged.
    """

    service = DiscoveryService(session)
    job, created = service.start_classification(user=context.user)
    return DiscoveryRunStartOut(
        job=_job_out(job, service.jobs.latest_challenge(job.id)),
        created=created,
    )


@router.post(
    "/similarity",
    response_model=DiscoveryRunStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_similarity(
    payload: SimilarityBatchIn | None = None,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DiscoveryRunStartOut:
    """Queue the closed-label comparison of notices with confirmed capabilities.

    The request only enqueues; the list endpoint never waits on a model. A
    notice the company cannot see is not judged, so the endpoint cannot be used
    to probe the corpus by id.
    """

    settings = get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable(
            "Tender discovery is not installed in this deployment."
        )
    # Check before enqueuing so the user gets the actionable message in the
    # request, not a job that fails after a poll. The handler repeats the check
    # because a capability can be revoked between enqueue and run.
    if not CompanyUnderstandingRepository(session).confirmed_capabilities(
        context.company.id
    ):
        raise BadRequest(
            "Confirm at least one capability from your company profile "
            "before asking the AI to compare tenders."
        )
    service = TenderService(session)
    requested = list(payload.tender_ids) if payload else []
    requested = requested[: settings.similarity_max_tenders]
    if requested:
        ids: list[uuid.UUID] = []
        for tender_id in requested:
            tender = service.get(tender_id)
            verdict = service.verdict_for(context.company, tender)
            if (
                verdict.get("bucket") == "hidden"
                and tender.source_type != "manual_upload"
            ):
                continue
            ids.append(tender_id)
    else:
        rows, _total = service.list(
            company=context.company,
            sort="match_desc",
            limit=settings.similarity_max_tenders,
        )
        ids = [row.id for row in rows]
    if not ids:
        raise BadRequest("There are no visible tenders to compare.")

    jobs = JobService(session)
    active = jobs.list_visible(
        context.company.id,
        states=["queued", "running"],
        kind="judge_tender_similarity",
        limit=1,
    )
    if active:
        job, created = active[0], False
    else:
        job, created = jobs.enqueue(
            kind="judge_tender_similarity",
            company_id=context.company.id,
            priority=65,
            payload={
                "tender_ids": [str(item) for item in ids],
                "requested_by": str(context.user.id),
            },
        )
    return DiscoveryRunStartOut(
        job=_job_out(job, jobs.latest_challenge(job.id)),
        created=created,
    )


@fetch_router.post(
    "/{tender_id}/documents/fetch",
    response_model=DocumentFetchStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def fetch_tender_documents(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> DocumentFetchStartOut:
    """Queue the global document fetch for one tender; never blocks the request."""

    job, created = enqueue_fetch(
        session,
        tender_id=tender_id,
        company_id=context.company.id,
        user_id=context.user.id,
    )
    service = JobService(session)
    return DocumentFetchStartOut(
        job=_job_out(job, service.latest_challenge(job.id)),
        created=created,
    )


@fetch_router.post(
    "/{tender_id}/refresh",
    response_model=TenderRefreshStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def refresh_tender_source(
    tender_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> TenderRefreshStartOut:
    """Queue one source recheck for a known tender; never blocks the request."""

    settings = get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable("Tender refresh is not enabled in this deployment.")
    tenders = TenderRepository(session)
    tender = tenders.get(tender_id)
    if tender is None:
        raise NotFound("Tender not found.")
    if tenders.locator(tender_id) is None:
        raise BadRequest(
            "This tender has no portal locator, so its source cannot be "
            "rechecked. Manually uploaded tenders are refreshed by uploading a "
            "newer document."
        )

    existing = JobRepository(session).active_for_payload(
        "refresh_tender", "tender_id", str(tender_id)
    )
    if existing is not None:
        service = JobService(session)
        return TenderRefreshStartOut(
            job=_job_out(existing, service.latest_challenge(existing.id)),
            created=False,
        )

    job, created = JobService(session, settings).enqueue(
        kind="refresh_tender",
        company_id=None,
        lane="browser",
        priority=45,
        payload={
            "tender_id": str(tender_id),
            "company_id": str(context.company.id),
            "user_id": str(context.user.id),
        },
    )
    service = JobService(session)
    return TenderRefreshStartOut(
        job=_job_out(job, service.latest_challenge(job.id)),
        created=created,
    )
