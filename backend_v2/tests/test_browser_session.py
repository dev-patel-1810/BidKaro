"""The thread that owns a browser session, and the attempt budget it spends.

Playwright's sync API belongs to the thread that created it. These tests use a
double that enforces the same rule, so a session used from the wrong thread
fails here instead of failing later with greenlet's message.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest

from app.core.db import SessionLocal
from app.core.time import utcnow
from app.discovery import sessions
from app.discovery.browser import SESSION_LOST_NOTE, SessionLost
from app.repositories.work import JobRepository


class TargetClosedError(Exception):
    """The shape of Playwright's error when the target is gone."""


class OwnerOnlyDriver:
    """Records the thread it was created on and refuses every other thread."""

    def __init__(self) -> None:
        self.created_on = threading.get_ident()
        self.calls: list[int] = []
        self.lost = False

    def touch(self) -> int:
        assert threading.get_ident() == self.created_on, (
            "the browser was used from a thread that does not own it"
        )
        self.calls.append(threading.get_ident())
        return self.created_on

    def close(self) -> None:
        self.touch()


def test_the_lane_runs_the_body_on_the_thread_that_created_the_driver():
    lane = sessions.SessionLane(job_id=uuid.uuid4(), factory=OwnerOnlyDriver)
    try:
        owner = lane.call(lambda driver: driver.touch())
        lane.call(lambda driver: driver.touch())
    finally:
        lane.shutdown()

    assert owner != threading.get_ident()
    assert lane.driver is not None
    assert set(lane.driver.calls) == {owner}


def test_a_closed_target_becomes_a_lost_session_not_an_error_to_retry():
    def body(_driver):
        raise TargetClosedError("Target page, context or browser has been closed")

    lane = sessions.SessionLane(job_id=uuid.uuid4(), factory=OwnerOnlyDriver)
    try:
        with pytest.raises(SessionLost) as raised:
            lane.call(body)
    finally:
        lane.shutdown()

    # The handler reports this as a partial with a human note. A raw closed
    # target would be retried by the worker, and a retry of a dead window can
    # only ask for another captcha.
    assert str(raised.value) == SESSION_LOST_NOTE
    assert lane.lost is True


def test_a_lane_that_was_closed_refuses_more_work():
    lane = sessions.SessionLane(job_id=uuid.uuid4(), factory=OwnerOnlyDriver)
    lane.call(lambda driver: driver.touch())
    lane.shutdown()

    with pytest.raises(SessionLost):
        lane.call(lambda driver: driver.touch())


def test_an_answered_challenge_does_not_spend_the_attempt_budget():
    with SessionLocal() as session:
        jobs = JobRepository(session)
        job, created = jobs.enqueue(
            kind="fetch_documents", payload={"tender_id": str(uuid.uuid4())}, lane="browser"
        )
        assert created is True
        session.commit()

        claimed = jobs.claim_next(worker_id="worker-1", lease_seconds=60)
        assert claimed is not None and claimed.id == job.id
        assert job.attempts == 1

        jobs.park(
            job.id,
            challenge_kind="captcha",
            context={},
            image_path=None,
            expires_at=utcnow() + timedelta(minutes=5),
        )
        session.commit()
        assert job.state == "needs_you"

        # The human answered and pressed Continue. That is progress, not a
        # failed attempt: two captchas used to exhaust a job that never failed.
        jobs.resume_after_challenge(job.id)
        session.commit()

        assert job.state == "queued"
        assert job.attempts == 0


class RecordingCDP:
    """A CDP session that records the window commands the driver sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def send(self, method: str, params: dict | None = None):
        self.sent.append((method, params or {}))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        return {}


class RecordingContext:
    def __init__(self, cdp: RecordingCDP) -> None:
        self._cdp = cdp

    def new_cdp_session(self, _page) -> RecordingCDP:
        return self._cdp


class RecordingPage:
    """A page that records what the driver asked it to draw or resize."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.viewports: list[dict] = []
        self.front = 0
        self.gotoed: list[str] = []
        self.cdp = RecordingCDP()
        self.context = RecordingContext(self.cdp)

    @property
    def url(self) -> str:
        return "https://etenders.gov.in/eprocure/app"

    def evaluate(self, script: str):
        self.scripts.append(script)
        return True

    def set_viewport_size(self, size: dict) -> None:
        self.viewports.append(dict(size))

    def bring_to_front(self) -> None:
        self.front += 1

    def goto(self, url: str, **_kwargs) -> None:
        self.gotoed.append(url)

    def inner_text(self, _selector: str) -> str:
        return "notice"

    def title(self) -> str:
        return "Notice"

    def content(self) -> str:
        return "<html><body>notice</body></html>"


class BrokenContext:
    def new_cdp_session(self, _page):
        raise RuntimeError("no window to minimize")


def _driver_with(page):
    from app.discovery.browser import LocalBrowserDriver

    driver = LocalBrowserDriver(headless=False)
    driver._page = page
    return driver


def test_work_mode_minimizes_and_covers_the_window_and_reveal_undoes_it():
    page = RecordingPage()
    driver = _driver_with(page)

    driver.begin_work()
    assert (
        "Browser.setWindowBounds",
        {"windowId": 7, "bounds": {"windowState": "minimized"}},
    ) in page.cdp.sent
    assert page.viewports == []  # minimized, not merely shrunk
    assert "bidkaro-working" in page.scripts[-1]
    assert page.front == 0  # working must not steal the screen
    assert driver._human_mode is False

    driver.reveal()
    assert (
        "Browser.setWindowBounds",
        {"windowId": 7, "bounds": {"windowState": "normal"}},
    ) in page.cdp.sent
    assert page.viewports[-1] == {"width": 1280, "height": 920}
    assert "bidkaro-working-style" in page.scripts[-1]
    assert page.front == 1
    assert driver._human_mode is True


def test_work_mode_shrinks_the_window_when_it_cannot_be_minimized():
    page = RecordingPage()
    page.context = BrokenContext()
    driver = _driver_with(page)

    driver.begin_work()
    assert page.viewports[-1] == {"width": 480, "height": 360}
    assert "bidkaro-working" in page.scripts[-1]
    assert page.front == 0
    assert driver.lost is False  # no window is not a lost session


def test_a_page_load_does_not_raise_the_window_while_the_software_works():
    page = RecordingPage()
    driver = _driver_with(page)

    driver.begin_work()
    driver.open("https://etenders.gov.in/eprocure/app")
    assert page.front == 0

    driver.reveal()
    driver.open("https://etenders.gov.in/eprocure/app")
    # ``reveal`` raised it, and a page load for the human raises it again.
    assert page.front == 2

