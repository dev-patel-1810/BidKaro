from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict, NotFound
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.work import Challenge, Job, JobRun
from app.repositories.work import (
    AppStateRepository,
    ChallengeRepository,
    JobRepository,
    RunRepository,
)
from app.services.job_events import notify_job_closed

JOB_KINDS = {
    "scrape_portal",
    "sweep_portal",
    "classify_corpus",
    "fetch_documents",
    "refresh_tender",
    "extract_requirements",
    "extract_company_document",
    "judge_tender_similarity",
    "snapshot_tender",
    "revalidate_citations",
    "send_reminders",
}
ACTIVE_STATES = {"queued", "running", "needs_you"}
TERMINAL_STATES = {"done", "partial", "failed", "cancelled"}


class JobService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.jobs = JobRepository(session)
        self.challenges = ChallengeRepository(session)
        self.runs = RunRepository(session)
        self.state = AppStateRepository(session)

    # -- jobs -------------------------------------------------------------

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        company_id: uuid.UUID | None = None,
        lane: str | None = None,
        priority: int = 100,
        idempotency_key: str | None = None,
    ) -> tuple[Job, bool]:
        if kind not in JOB_KINDS:
            raise BadRequest(
                "Choose a job kind from: " + ", ".join(sorted(JOB_KINDS)) + "."
            )
        if lane not in {None, "browser"}:
            raise BadRequest("The only shared lane is 'browser'.")
        job, created = self.jobs.enqueue(
            kind=kind,
            payload=payload or {},
            company_id=company_id,
            lane=lane,
            priority=priority,
            idempotency_key=idempotency_key,
            max_attempts=self.settings.job_max_attempts,
        )
        self.session.commit()
        return job, created

    def get(self, job_id: uuid.UUID) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFound("Job not found.")
        return job

    def get_visible(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        job = self.get(job_id)
        if job.company_id is not None and job.company_id != company_id:
            # Cross-tenant reads are 404, never 403 (blueprint §5.1).
            raise NotFound("Job not found.")
        return job

    def list_visible(
        self,
        company_id: uuid.UUID,
        *,
        states: list[str] | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        if states and any(state not in ACTIVE_STATES | TERMINAL_STATES for state in states):
            raise BadRequest("Unknown job state.")
        if kind is not None and kind not in JOB_KINDS:
            raise BadRequest("Unknown job kind.")
        return self.jobs.list(
            company_id=company_id, states=states, kind=kind, limit=limit
        )

    def cancel(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        job = self.get_visible(job_id, company_id)
        if job.state in TERMINAL_STATES:
            raise Conflict("This job has already finished.")
        try:
            job = self.jobs.cancel(job.id) or job
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        notify_job_closed(job.kind, job.id)
        return job

    def retry(self, job_id: uuid.UUID, company_id: uuid.UUID) -> Job:
        job = self.get_visible(job_id, company_id)
        if job.state not in {"failed", "partial", "cancelled"}:
            raise Conflict("Only a finished job can be retried.")
        try:
            job = self.jobs.retry(job.id) or job
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        notify_job_closed(job.kind, job.id)
        return job

    def runs_for(self, job_id: uuid.UUID, company_id: uuid.UUID) -> list[JobRun]:
        self.get_visible(job_id, company_id)
        return self.runs.list_for_job(job_id)

    # -- challenges -------------------------------------------------------

    def signal_challenge(
        self, job_id: uuid.UUID, company_id: uuid.UUID
    ) -> Challenge:
        """Record that the human acted. There is deliberately no answer."""

        job = self.get_visible(job_id, company_id)
        if job.state != "needs_you":
            raise Conflict("This job is not waiting for you.")
        challenge = self.challenges.open_for_job(job.id)
        if challenge is None:
            raise Conflict("This job is not waiting for you.")
        now = utcnow()
        if challenge.expires_at <= now:
            self._expire_one(challenge, now)
            self.session.commit()
            raise Conflict("The request expired. Retry the job.")
        try:
            challenge = self.challenges.signal(challenge.id, now=now) or challenge
            self.jobs.resume_after_challenge(job.id)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return challenge

    def needs_you(self, company_id: uuid.UUID) -> list[Job]:
        return self.list_visible(company_id, states=["needs_you"], limit=50)

    def latest_challenge(self, job_id: uuid.UUID) -> Challenge | None:
        return self.challenges.latest_for_job(job_id)

    # -- maintenance (scheduler calls these) -----------------------------

    def recover_stale(self) -> int:
        count = self.jobs.recover_stale()
        self.session.commit()
        return count

    def expire_challenges(self) -> int:
        now = utcnow()
        expired = self.challenges.expire_due(now=now)
        for challenge in expired:
            self._expire_one(challenge, now)
        self.session.commit()
        return len(expired)

    def _expire_one(self, challenge: Challenge, now: datetime) -> None:
        challenge.state = "expired"
        if challenge.job_id is None:
            return
        job = self.jobs.get(challenge.job_id, for_update=True)
        if job is None or job.state != "needs_you":
            return
        job.state = "partial"
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = "file request expired"
        job.updated_at = now
        self.jobs.release_lane(job)
        notify_job_closed(job.kind, job.id)
        run = self.runs.latest_for_job(job.id)
        if run is not None and run.status in ACTIVE_STATES:
            self.runs.finish(
                run,
                status="partial",
                metrics=run.metrics,
                notes="file request expired",
            )

    def resume_answered(self) -> int:
        answered = self.session.scalars(
            select(Challenge).where(
                Challenge.state == "answered", Challenge.job_id.is_not(None)
            )
        ).all()
        resumed = 0
        for challenge in answered:
            job = self.jobs.get(challenge.job_id, for_update=True)
            if job is not None and job.state == "needs_you":
                job.state = "queued"
                job.run_after = utcnow()
                job.updated_at = utcnow()
                # An answered challenge is progress, not a failed attempt.
                job.attempts = 0
                resumed += 1
        self.session.commit()
        return resumed

    # -- global scheduler fact -------------------------------------------

    def last_completed_run_at(self) -> datetime | None:
        return self.state.get_datetime("last_completed_run_at")

    def mark_completed(self, *, when: datetime | None = None) -> None:
        self.state.set_datetime("last_completed_run_at", when or utcnow())
        self.session.commit()

    def scheduler_due(self, *, now: datetime | None = None) -> bool:
        now = now or utcnow()
        last = self.last_completed_run_at()
        if last is None:
            return True
        return now - last >= timedelta(hours=self.settings.scrape_interval_hours)
