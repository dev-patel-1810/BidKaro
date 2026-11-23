from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, get_auth_context
from app.core.db import get_db
from app.core.errors import NotFound
from app.core.settings import get_settings
from app.models.work import Challenge, Job
from app.schemas.jobs import (
    ChallengeAnswerIn,
    ChallengeAnswerOut,
    ChallengeOut,
    JobOut,
    RunOut,
)
from app.services.jobs import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _challenge_out(challenge: Challenge | None) -> ChallengeOut | None:
    if challenge is None:
        return None
    image_url = None
    if challenge.image_path:
        image_url = (
            f"/api/jobs/{challenge.job_id}/captures/"
            f"{Path(challenge.image_path).name}"
        )
    return ChallengeOut(
        id=challenge.id,
        job_id=challenge.job_id,
        run_id=challenge.run_id,
        kind=challenge.kind,
        image_url=image_url,
        context=challenge.context or {},
        state=challenge.state,
        opened_at=challenge.opened_at,
        expires_at=challenge.expires_at,
        answered_at=challenge.answered_at,
    )


def _job_out(job: Job, challenge: Challenge | None) -> JobOut:
    return JobOut.model_validate(job).model_copy(
        update={"challenge": _challenge_out(challenge)}
    )


@router.get("/needs-you")
def list_needs_you(
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    service = JobService(session)
    jobs = service.needs_you(context.company.id)
    items = [
        _job_out(job, service.latest_challenge(job.id))
        for job in jobs
    ]
    return {"items": items, "next": None}


@router.get("")
def list_jobs(
    state: list[str] | None = Query(default=None),
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    service = JobService(session)
    jobs = service.list_visible(
        context.company.id, states=state, kind=kind, limit=limit
    )
    items = [
        _job_out(job, service.latest_challenge(job.id))
        for job in jobs
    ]
    return {"items": items, "next": None}


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    service = JobService(session)
    job = service.get_visible(job_id, context.company.id)
    return _job_out(job, service.latest_challenge(job.id))


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(
    job_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    service = JobService(session)
    job = service.cancel(job_id, context.company.id)
    return _job_out(job, service.latest_challenge(job.id))


@router.post("/{job_id}/retry", response_model=JobOut)
def retry_job(
    job_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> JobOut:
    service = JobService(session)
    job = service.retry(job_id, context.company.id)
    return _job_out(job, service.latest_challenge(job.id))


@router.post("/{job_id}/challenge", response_model=ChallengeOut)
def signal_challenge(
    job_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChallengeOut:
    """Signal that the human acted outside the app. No answer is accepted."""

    service = JobService(session)
    challenge = service.signal_challenge(job_id, context.company.id)
    return _challenge_out(challenge)  # type: ignore[return-value]


@router.post("/{job_id}/challenge/answer", response_model=ChallengeAnswerOut)
def answer_challenge(
    job_id: uuid.UUID,
    body: ChallengeAnswerIn,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> ChallengeAnswerOut:
    """Carry the human's answer into the portal's form once and drop it.

    The answer is not stored, logged or returned. An answer the portal rejects
    keeps the job parked and refreshes the picture, so the next attempt uses
    the portal's new challenge.
    """

    from app.discovery.answering import answer_challenge as submit_answer

    accepted = submit_answer(job_id, context.company.id, body.answer)
    service = JobService(session)
    job = service.get_visible(job_id, context.company.id)
    return ChallengeAnswerOut(
        accepted=accepted,
        job=_job_out(job, service.latest_challenge(job.id)),
    )


@router.get("/{job_id}/runs")
def list_runs(
    job_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> dict:
    service = JobService(session)
    runs = service.runs_for(job_id, context.company.id)
    return {
        "items": [RunOut.model_validate(run) for run in runs],
        "next": None,
    }


@router.get("/{job_id}/captures/{filename}")
def get_capture(
    job_id: uuid.UUID,
    filename: str,
    context: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> FileResponse:
    JobService(session).get_visible(job_id, context.company.id)
    settings = get_settings()
    base = (settings.storage_dir / "captures" / str(job_id)).resolve()
    candidate = (base / filename).resolve()
    if base not in candidate.parents or not candidate.is_file():
        raise NotFound("Capture not found.")
    return FileResponse(candidate)
