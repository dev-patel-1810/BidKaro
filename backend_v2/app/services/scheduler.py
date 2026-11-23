from __future__ import annotations

import logging
import threading

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.db import SessionLocal
from app.core.settings import Settings, get_settings
from app.services.job_registry import get_handlers
from app.services.jobs import JobService

logger = logging.getLogger(__name__)


class Scheduler:
    """The startup catch-up and periodic maintenance loop.

    The scheduler is a **job producer**, not a second execution path: it enqueues
    a ``scrape_portal`` job and the same worker that runs everything else claims
    it. ``last_completed_run_at`` is global because the corpus is global.
    """

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop.wait(self.settings.scheduler_tick_seconds)

    def tick(self) -> bool:
        """Run one tick. Return True when a scheduled scrape was enqueued."""

        enqueued = self._enqueue_if_due()
        with self.session_factory() as session:
            service = JobService(session, self.settings)
            service.recover_stale()
            service.expire_challenges()
            service.resume_answered()
        return enqueued

    def _enqueue_if_due(self) -> bool:
        handlers = get_handlers()
        # The nightly job is the sweep: it builds the corpus from the portal
        # itself. Run discovery stays available as a manual, profile-first
        # fallback, so a deployment that never installed the sweep keeps the
        # walk on its old schedule.
        kind = "sweep_portal" if "sweep_portal" in handlers else "scrape_portal"
        if kind not in handlers:
            # The base has no scraper. Do not enqueue work nobody can run.
            return False
        with self.session_factory() as session:
            # The advisory lock makes check-and-enqueue atomic across processes.
            locked = session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext('bidkaro_scheduler'))")
            )
            if not locked:
                return False
            service = JobService(session, self.settings)
            if not service.scheduler_due():
                session.rollback()
                return False
            if service.jobs.has_active("scrape_portal") or service.jobs.has_active(
                "sweep_portal"
            ):
                # One browser owner at a time: the nightly sweep must not start
                # under a walk the user is watching, or the other way round.
                session.rollback()
                return False
            service.enqueue(
                kind=kind,
                payload={"trigger": "scheduled"},
                lane="browser",
                priority=200,
            )
            return True
