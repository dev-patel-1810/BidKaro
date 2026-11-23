from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from datetime import timedelta
from typing import Any
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.db import SessionLocal
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.work import JobRun
from app.repositories.work import ChallengeRepository, JobRepository, RunRepository
from app.services.job_events import notify_job_closed
from app.services.job_registry import get_handlers
from app.services.job_types import JobCancelled, JobParked, JobResult
from app.services.jobs import JobService

logger = logging.getLogger(__name__)


class JobContext:
    """Everything a handler may do, and nothing else.

    A handler never touches SQLAlchemy or the request session. Heartbeats,
    cancellation checks, parking and completion all go through short-lived
    sessions, so a long browser walk cannot hold a transaction open.
    """

    def __init__(
        self,
        *,
        runner: "JobRunner",
        job_id: uuid.UUID,
        company_id: uuid.UUID | None,
        kind: str,
        payload: dict[str, Any],
        attempts: int,
        run_id: uuid.UUID | None,
    ) -> None:
        self.runner = runner
        self.job_id = job_id
        self.company_id = company_id
        self.kind = kind
        self.payload = payload
        self.attempts = attempts
        self.run_id = run_id
        self._session_factory = runner.session_factory

    def heartbeat(self) -> None:
        with self._session_factory() as session:
            jobs = JobRepository(session)
            job = jobs.get(self.job_id)
            if job is None or job.state == "cancelled":
                # Cancellation is checked on the same cheap round trip as the
                # lease, so a walk or fetch stops at its next step instead of
                # running to the end after the human pressed Cancel.
                raise JobCancelled("The job was cancelled.")
            jobs.heartbeat(
                self.job_id,
                worker_id=self.runner.worker_id,
                lease_seconds=self.runner.settings.job_lease_seconds,
            )
            session.commit()

    def cancelled(self) -> bool:
        with self._session_factory() as session:
            job = JobRepository(session).get(self.job_id)
            return job is None or job.state == "cancelled"

    def park(
        self,
        *,
        kind: str,
        context: dict[str, Any] | None = None,
        image_path: str | None = None,
        expires_at=None,
    ) -> None:
        """Wait for a human. Raises to unwind the handler without failing it."""

        now = utcnow()
        expires = expires_at or (
            now + timedelta(seconds=self.runner.settings.agent_challenge_wait_seconds)
        )
        with self._session_factory() as session:
            jobs = JobRepository(session)
            challenge = jobs.park(
                self.job_id,
                challenge_kind=kind,
                context=context or {},
                image_path=image_path,
                expires_at=expires,
                run_id=self.run_id,
            )
            if challenge is None:
                raise RuntimeError("The job could not be parked.")
            if self.run_id is not None:
                run = RunRepository(session).latest_for_job(self.job_id)
                if run is not None:
                    run.status = "needs_you"
            session.commit()
            challenge_id = challenge.id
        raise JobParked(challenge_id)

    def mark_completed(self) -> None:
        with self._session_factory() as session:
            JobService(session, self.runner.settings).mark_completed()

    def latest_challenge(self):
        with self._session_factory() as session:
            return ChallengeRepository(session).latest_for_job(self.job_id)


class JobRunner:
    """A small daemon worker pool.

    Workers are daemon threads so a walk in progress never keeps the process
    alive at shutdown. The durable record is the job row; a killed worker is
    recovered by the lease expiry, not by memory.
    """

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        *,
        settings: Settings | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        for index in range(self.settings.job_workers):
            thread = threading.Thread(
                target=self._loop,
                name=f"job-worker-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("Job worker failed while claiming or running a job")
                worked = False
            if not worked:
                self._stop.wait(self.settings.job_poll_seconds)

    def run_once(self) -> bool:
        with self.session_factory() as session:
            jobs = JobRepository(session)
            try:
                job = jobs.claim_next(
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.job_lease_seconds,
                )
            except IntegrityError:
                # Two workers raced to create the same lane row; try again.
                session.rollback()
                return False
            if job is None:
                session.rollback()
                return False
            run = RunRepository(session).start(
                kind=job.kind, job_id=job.id, company_id=job.company_id
            )
            job_id = job.id
            company_id = job.company_id
            kind = job.kind
            payload = dict(job.payload or {})
            attempts = job.attempts
            max_attempts = job.max_attempts
            run_id = run.id
            session.commit()

        handler = get_handlers().get(kind)
        if handler is None:
            self._fail(job_id, run_id, "No handler is registered for this job kind.")
            notify_job_closed(kind, job_id)
            return True

        context = JobContext(
            runner=self,
            job_id=job_id,
            company_id=company_id,
            kind=kind,
            payload=payload,
            attempts=attempts,
            run_id=run_id,
        )
        try:
            #result = handler(context) or JobResult()
	        result = handler(context) or JobResult()
        except JobParked:
            return True
        except JobCancelled:
            # The user pressed Cancel while the handler was running. The job
            # row is already cancelled; finish its run and close the browser.
            with self.session_factory() as session:
                JobRepository(session).finish(job_id, state="cancelled")
                self._finish_run(session, run_id, "cancelled")
                session.commit()
            notify_job_closed(kind, job_id)
            return True
        except Exception as exc:  # a handler failure is recorded, never swallowed
            logger.exception("Job %s failed", job_id)
            if attempts < max_attempts:
                delay = min(60, 5 * attempts)
                with self.session_factory() as session:
                    JobRepository(session).requeue(
                        job_id,
                        run_after=utcnow() + timedelta(seconds=delay),
                        error=str(exc)[:2000],
                    )
                    self._finish_run(session, run_id, "failed", notes=str(exc)[:2000])
                    session.commit()
            else:
                self._fail(job_id, run_id, str(exc)[:2000])
            notify_job_closed(kind, job_id)
            return True

        if context.cancelled():
            with self.session_factory() as session:
                JobRepository(session).finish(job_id, state="cancelled")
                self._finish_run(session, run_id, "cancelled")
                session.commit()
            notify_job_closed(kind, job_id)
            return True

        state = result.state if result.state in {"done", "partial"} else "done"
        with self.session_factory() as session:
            JobRepository(session).finish(job_id, state=state)
            self._finish_run(
                session,
                run_id,
                state,
                metrics=result.metrics,
                notes=result.notes,
            )
            session.commit()
        notify_job_closed(kind, job_id)
        if kind == "scrape_portal" and state in {"done", "partial"}:
            context.mark_completed()
        elif kind == "sweep_portal" and state == "done":
            # A full sweep resets the nightly clock; a partial one does not,
            # because it did not cover the directory and is still due.
            context.mark_completed()
        return True

    def _fail(self, job_id: uuid.UUID, run_id: uuid.UUID, error: str) -> None:
        with self.session_factory() as session:
            JobRepository(session).finish(job_id, state="failed", error=error)
            self._finish_run(session, run_id, "failed", notes=error)
            session.commit()

    def _finish_run(
        self,
        session,
        run_id: uuid.UUID,
        status: str,
        *,
        metrics: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        run = session.get(JobRun, run_id)
        if run is not None:
            RunRepository(session).finish(
                run, status=status, metrics=metrics, notes=notes
            )
