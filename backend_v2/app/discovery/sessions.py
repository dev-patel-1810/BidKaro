"""The browser sessions a parked job keeps alive, and the thread that owns them.

A ``needs_you`` job releases its worker thread but must keep the browser
session: the portal binds both a document and a challenge to the session that
opened it. The registry is process-local by design. If the process dies, the
session is gone and the job resumes honestly as a new attempt instead of
pretending the old one survived.

Playwright's sync objects belong to the thread that created them, and the
download a human's answer produces is only delivered while that thread is
running the page's event loop. So a session is not a driver in a dictionary;
it is a thread with a queue. The whole job body runs on that thread, the
worker thread only waits for the result, and a resume after ``needs_you``
reaches the same thread even when a different worker claims the job.
"""

from __future__ import annotations

import queue
import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from app.discovery.browser import (
    SESSION_LOST_NOTE,
    BrowserDriver,
    SessionLost,
    is_closed_error,
)

T = TypeVar("T")


@dataclass(slots=True)
class FetchSession:
    job_id: uuid.UUID
    tender_id: uuid.UUID
    driver: BrowserDriver
    lane: "SessionLane | None" = None
    links: list[dict[str, Any]] = field(default_factory=list)
    index: int = 0
    documents: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending_link: dict[str, Any] | None = None
    answered_challenges: int = 0
    #: Every picture shown in this fetch, including a rejected answer's new
    #: one. A portal cannot make the user answer pictures forever.
    challenges_shown: int = 0
    rejected_answers: int = 0
    rejected_link: str = ""
    created_at: float = 0.0
    extracted: set[str] = field(default_factory=set)


@dataclass(slots=True)
class WalkSession:
    """A portal walk that is waiting for a human in its own browser.

    The walk is restarted from the directory after the human answers, in the
    same browser, so the portal recognises the session. ``answered_challenges``
    counts the handoffs already made: a second challenge is reported rather
    than parked again, because the software never loops on a captcha.
    """

    job_id: uuid.UUID
    driver: BrowserDriver
    lane: "SessionLane | None" = None
    created_at: float = 0.0
    answered_challenges: int = 0


SessionRecord = FetchSession | WalkSession


class SessionLane:
    """One thread that owns one browser session.

    ``call`` runs the whole job body on the owner thread and returns its
    result, so the body may use ``_page`` directly as it always did. The lane
    exists for as long as the session does, including across ``needs_you``.

    A lane built without a factory is *inline*: it runs the body on the calling
    thread. That is only used for a session handed straight to the registry by
    a test; every real session gets a thread.
    """

    def __init__(
        self,
        *,
        job_id: uuid.UUID | None = None,
        factory: Callable[[], BrowserDriver] | None = None,
        name: str = "browser-lane",
    ) -> None:
        self.job_id = job_id
        self.session: SessionRecord | None = None
        self._factory = factory
        self._driver: BrowserDriver | None = None
        self._queue: queue.Queue = queue.Queue()
        self._closed = threading.Event()
        self._lost = False
        self._thread: threading.Thread | None = None
        if factory is not None:
            suffix = job_id.hex[:8] if job_id is not None else uuid.uuid4().hex[:8]
            self._thread = threading.Thread(
                target=self._run,
                name=f"{name}-{suffix}",
                daemon=True,
            )
            self._thread.start()

    @classmethod
    def inline(cls, driver: BrowserDriver) -> "SessionLane":
        lane = cls()
        lane._driver = driver
        return lane

    @property
    def driver(self) -> BrowserDriver | None:
        return self._driver

    @property
    def lost(self) -> bool:
        """Whether this session has ended, however the driver found out."""

        if self._lost:
            return True
        return bool(getattr(self._driver, "lost", False))

    def call(self, fn: Callable[[BrowserDriver], T]) -> T:
        """Run ``fn`` with the session's driver on the owner thread."""

        if self._closed.is_set():
            raise SessionLost(SESSION_LOST_NOTE)
        if self._thread is None:
            return fn(self._require_driver())
        if threading.current_thread() is self._thread:
            raise RuntimeError("A browser session cannot call into its own thread.")
        future: Future = Future()
        self._queue.put((future, fn))
        return future.result()

    def _require_driver(self) -> BrowserDriver:
        if self._driver is None:
            raise SessionLost(SESSION_LOST_NOTE)
        return self._driver

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if self._driver is None:
                    if self._factory is None:
                        raise SessionLost(SESSION_LOST_NOTE)
                    self._driver = self._factory()
                future.set_result(fn(self._driver))
            except BaseException as exc:  # noqa: BLE001 - the caller decides
                if is_closed_error(exc):
                    # A closed target is a lost session wherever it surfaces,
                    # so no handler can turn it into a retry of a dead browser.
                    self._lost = True
                    future.set_exception(SessionLost(SESSION_LOST_NOTE))
                else:
                    future.set_exception(exc)
            if self._closed.is_set():
                return

    def shutdown(self, timeout: float = 5.0) -> None:
        """Close the driver on its own thread and stop the lane.

        Safe to call twice and safe from any thread, including the owner.
        """

        self._closed.set()
        if self._thread is None:
            self._close_driver()
            return
        if threading.current_thread() is self._thread:
            self._close_driver()
            return
        if self._thread.is_alive():
            future: Future = Future()
            self._queue.put((future, lambda driver: driver.close()))
            try:
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - a stuck page must not hang shutdown
                pass
        self._queue.put(None)
        self._thread.join(timeout=timeout)

    def _close_driver(self) -> None:
        driver = self._driver
        if driver is None:
            return
        try:
            driver.close()
        except Exception:  # noqa: BLE001 - closing must never fail a job
            pass


_lane_lock = threading.Lock()
_lanes: dict[uuid.UUID, SessionLane] = {}


def get(job_id: uuid.UUID) -> SessionRecord | None:
    with _lane_lock:
        lane = _lanes.get(job_id)
    return lane.session if lane is not None else None


def lane(job_id: uuid.UUID) -> SessionLane | None:
    with _lane_lock:
        return _lanes.get(job_id)


def open_lane(
    job_id: uuid.UUID,
    factory: Callable[[], BrowserDriver],
    *,
    name: str = "browser-lane",
) -> SessionLane:
    """Start the owner thread for a job that has no session yet."""

    opened = SessionLane(job_id=job_id, factory=factory, name=name)
    with _lane_lock:
        _lanes[job_id] = opened
    return opened


def put(session: SessionRecord, lane_record: SessionLane | None = None) -> SessionLane:
    """Register a session, keeping the lane it already belongs to."""

    owner = lane_record or session.lane or SessionLane.inline(session.driver)
    owner.session = session
    session.lane = owner
    with _lane_lock:
        _lanes[session.job_id] = owner
    return owner


def pop(job_id: uuid.UUID) -> SessionLane | None:
    with _lane_lock:
        return _lanes.pop(job_id, None)


def close(job_id: uuid.UUID) -> None:
    owner = pop(job_id)
    if owner is not None:
        owner.shutdown()


def close_all() -> None:
    with _lane_lock:
        owners = list(_lanes.values())
        _lanes.clear()
    for owner in owners:
        owner.shutdown()


def count() -> int:
    with _lane_lock:
        return len(_lanes)
