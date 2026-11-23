from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.work import AppState, Challenge, Job, JobRun, LaneLease


class AppStateRepository:
    """The small key/value table that holds facts like the last crawl time."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.session.get(AppState, key)
        return row.value if row is not None else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        row = self.session.get(AppState, key)
        if row is None:
            self.session.add(AppState(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()

    def get_datetime(self, key: str) -> datetime | None:
        value = self.get(key)
        if not value:
            return None
        raw = value.get("at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    def set_datetime(self, key: str, value: datetime) -> None:
        self.set(key, {"at": value.isoformat()})


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        company_id: uuid.UUID | None = None,
        lane: str | None = None,
        priority: int = 100,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        run_after: datetime | None = None,
    ) -> tuple[Job, bool]:
        """Return ``(job, created)``.

        A repeated idempotency key returns the existing row instead of creating a
        second one. The caller owns the commit so the key and the work it
        protects commit together (CODING-GUIDE §8).
        """

        if idempotency_key:
            existing = self.session.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing, False

        job = Job(
            company_id=company_id,
            kind=kind,
            lane=lane,
            payload=payload or {},
            priority=priority,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            run_after=run_after or utcnow(),
        )
        self.session.add(job)
        self.session.flush()
        return job, True

    def get(
        self, job_id: uuid.UUID, *, for_update: bool = False
    ) -> Job | None:
        statement = select(Job).where(Job.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def visible_to_company(self, company_id: uuid.UUID) -> Any:
        """System jobs (no company) are visible to every authenticated company."""

        return or_(Job.company_id == company_id, Job.company_id.is_(None))

    def list(
        self,
        *,
        company_id: uuid.UUID | None = None,
        states: list[str] | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        statement = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if company_id is not None:
            statement = statement.where(self.visible_to_company(company_id))
        if states:
            statement = statement.where(Job.state.in_(states))
        if kind:
            statement = statement.where(Job.kind == kind)
        return list(self.session.scalars(statement).all())

    def has_active(self, kind: str) -> bool:
        return (
            self.session.scalar(
                select(Job.id)
                .where(Job.kind == kind, Job.state.in_(["queued", "running", "needs_you"]))
                .limit(1)
            )
            is not None
        )

    def active_for_payload(
        self, kind: str, key: str, value: str
    ) -> Job | None:
        """One live job of this kind whose payload names the same object."""

        return self.session.scalar(
            select(Job)
            .where(
                Job.kind == kind,
                Job.state.in_(["queued", "running", "needs_you"]),
                Job.payload[key].astext == str(value),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> Job | None:
        """Claim one queued job and, for a browser job, its lane lease.

        The row lock is ``SKIP LOCKED`` so two workers never both claim the same
        row. A browser job whose lane is held by another owner is left queued.
        A job that already owns the lane (a resume after ``needs_you``) may claim
        it again.
        """

        now = now or utcnow()
        statement = (
            select(Job)
            .where(Job.state == "queued", Job.run_after <= now)
            .order_by(Job.priority.asc(), Job.created_at.asc())
            .limit(25)
            .with_for_update(skip_locked=True)
        )
        for job in self.session.scalars(statement).all():
            if job.lane:
                if not self._claim_lane(job, now, lease_seconds):
                    continue
            job.state = "running"
            job.attempts += 1
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.started_at = job.started_at or now
            job.updated_at = now
            self.session.flush()
            return job
        return None

    def _claim_lane(self, job: Job, now: datetime, lease_seconds: int) -> bool:
        lane = self.session.get(LaneLease, job.lane, with_for_update=True)
        if lane is None:
            lane = LaneLease(
                name=job.lane,
                owner_kind="job",
                owner_id=job.id,
                acquired_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                reserved_for_challenge=False,
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            self.session.add(lane)
            self.session.flush()
            return True

        owned_by_this_job = lane.owner_kind == "job" and lane.owner_id == job.id
        if not owned_by_this_job and lane.expires_at > now:
            return False

        lane.owner_kind = "job"
        lane.owner_id = job.id
        lane.acquired_at = now
        lane.lease_expires_at = now + timedelta(seconds=lease_seconds)
        lane.reserved_for_challenge = False
        lane.expires_at = now + timedelta(seconds=lease_seconds)
        return True

    def heartbeat(
        self, job_id: uuid.UUID, *, worker_id: str, lease_seconds: int
    ) -> bool:
        now = utcnow()
        job = self.get(job_id, for_update=True)
        if job is None or job.state != "running" or job.lease_owner != worker_id:
            return False
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.updated_at = now
        lane = self.session.get(LaneLease, job.lane, with_for_update=True) if job.lane else None
        if lane is not None and lane.owner_kind == "job" and lane.owner_id == job.id:
            lane.lease_expires_at = now + timedelta(seconds=lease_seconds)
            lane.expires_at = now + timedelta(seconds=lease_seconds)
        self.session.flush()
        return True

    def finish(
        self,
        job_id: uuid.UUID,
        *,
        state: str,
        error: str | None = None,
    ) -> Job | None:
        job = self.get(job_id, for_update=True)
        if job is None:
            return None
        if job.state == "cancelled":
            self.release_lane(job)
            return job
        job.state = state
        job.finished_at = utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = error
        job.updated_at = utcnow()
        self.release_lane(job)
        self.session.flush()
        return job

    def requeue(
        self,
        job_id: uuid.UUID,
        *,
        run_after: datetime,
        error: str,
    ) -> Job | None:
        job = self.get(job_id, for_update=True)
        if job is None:
            return None
        job.state = "queued"
        job.lease_owner = None
        job.lease_expires_at = None
        job.run_after = run_after
        job.last_error = error
        job.updated_at = utcnow()
        self.release_lane(job)
        self.session.flush()
        return job

    def cancel(self, job_id: uuid.UUID) -> Job | None:
        job = self.get(job_id, for_update=True)
        if job is None:
            return None
        if job.state in {"done", "partial", "failed", "cancelled"}:
            return job
        job.state = "cancelled"
        job.finished_at = utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = utcnow()
        for challenge in self.session.scalars(
            select(Challenge).where(
                Challenge.job_id == job.id, Challenge.state == "open"
            )
        ):
            challenge.state = "expired"
        self.release_lane(job)
        self.session.flush()
        return job

    def retry(self, job_id: uuid.UUID) -> Job | None:
        job = self.get(job_id, for_update=True)
        if job is None:
            return None
        if job.state not in {"failed", "partial", "cancelled"}:
            return job
        job.state = "queued"
        job.attempts = 0
        job.last_error = None
        job.finished_at = None
        job.run_after = utcnow()
        job.updated_at = utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        # A retry is fresh work, not the same idempotent request.
        job.idempotency_key = None
        self.session.flush()
        return job

    def park(
        self,
        job_id: uuid.UUID,
        *,
        challenge_kind: str,
        context: dict[str, Any],
        image_path: str | None,
        expires_at: datetime,
        run_id: uuid.UUID | None = None,
    ) -> Challenge | None:
        """Put the job in ``needs_you``. The lane stays reserved.

        The worker thread is released by returning to the pool; the browser
        session survives under the lane's bounded reservation.
        """

        job = self.get(job_id, for_update=True)
        if job is None or job.state != "running":
            return None
        now = utcnow()
        job.state = "needs_you"
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        challenge = Challenge(
            job_id=job.id,
            run_id=run_id,
            kind=challenge_kind,
            image_path=image_path,
            context=context,
            expires_at=expires_at,
        )
        self.session.add(challenge)
        if job.lane:
            lane = self.session.get(LaneLease, job.lane, with_for_update=True)
            if lane is not None and lane.owner_kind == "job" and lane.owner_id == job.id:
                lane.reserved_for_challenge = True
                lane.lease_expires_at = expires_at
                lane.expires_at = expires_at
        self.session.flush()
        return challenge

    def resume_after_challenge(self, job_id: uuid.UUID) -> Job | None:
        """Queue a parked job after the human signalled, keeping its lane."""

        job = self.get(job_id, for_update=True)
        if job is None or job.state != "needs_you":
            return None
        job.state = "queued"
        job.run_after = utcnow()
        job.updated_at = utcnow()
        # A handoff is progress, not a retry. Counting each answered challenge
        # against the attempt budget meant three captchas exhausted a job that
        # never failed; the budget is for errors, so it starts again here.
        job.attempts = 0
        self.session.flush()
        return job

    def release_lane(self, job: Job) -> None:
        if not job.lane:
            return
        lane = self.session.get(LaneLease, job.lane, with_for_update=True)
        if lane is not None and lane.owner_kind == "job" and lane.owner_id == job.id:
            self.session.delete(lane)
            self.session.flush()

    def recover_stale(self, *, now: datetime | None = None) -> int:
        """Return dead workers' jobs to the queue and drop their lane leases."""

        now = now or utcnow()
        stale = self.session.scalars(
            select(Job).where(
                Job.state == "running",
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < now,
            )
        ).all()
        for job in stale:
            job.state = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
            job.run_after = now
            job.last_error = "Worker stopped before the job finished."
            job.updated_at = now
            if job.lane:
                lane = self.session.get(LaneLease, job.lane, with_for_update=True)
                if lane is not None and lane.owner_kind == "job" and lane.owner_id == job.id:
                    self.session.delete(lane)
        self.session.flush()
        return len(stale)


class ChallengeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, challenge_id: uuid.UUID) -> Challenge | None:
        return self.session.get(Challenge, challenge_id)

    def latest_for_job(self, job_id: uuid.UUID) -> Challenge | None:
        return self.session.scalar(
            select(Challenge)
            .where(Challenge.job_id == job_id)
            .order_by(Challenge.created_at.desc())
            .limit(1)
        )

    def open_for_job(self, job_id: uuid.UUID) -> Challenge | None:
        return self.session.scalar(
            select(Challenge)
            .where(Challenge.job_id == job_id, Challenge.state == "open")
            .order_by(Challenge.created_at.desc())
            .limit(1)
        )

    def signal(self, challenge_id: uuid.UUID, *, now: datetime | None = None) -> Challenge | None:
        challenge = self.get(challenge_id)
        if challenge is None or challenge.state != "open":
            return None
        challenge.state = "answered"
        challenge.answered_at = now or utcnow()
        self.session.flush()
        return challenge

    def expire_due(self, *, now: datetime | None = None) -> list[Challenge]:
        now = now or utcnow()
        rows = self.session.scalars(
            select(Challenge).where(
                Challenge.state == "open", Challenge.expires_at <= now
            )
        ).all()
        for challenge in rows:
            challenge.state = "expired"
        self.session.flush()
        return list(rows)


class RunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(
        self,
        *,
        kind: str,
        job_id: uuid.UUID | None = None,
        company_id: uuid.UUID | None = None,
    ) -> JobRun:
        run = JobRun(
            job_id=job_id,
            company_id=company_id,
            kind=kind,
            status="running",
            started_at=utcnow(),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish(
        self,
        run: JobRun,
        *,
        status: str,
        metrics: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        run.status = status
        run.metrics = metrics or run.metrics
        run.notes = notes
        run.finished_at = utcnow()
        self.session.flush()

    def latest_for_job(self, job_id: uuid.UUID) -> JobRun | None:
        return self.session.scalar(
            select(JobRun)
            .where(JobRun.job_id == job_id)
            .order_by(JobRun.created_at.desc())
            .limit(1)
        )

    def list_for_job(self, job_id: uuid.UUID) -> list[JobRun]:
        return list(
            self.session.scalars(
                select(JobRun)
                .where(JobRun.job_id == job_id)
                .order_by(JobRun.created_at.desc())
            ).all()
        )
