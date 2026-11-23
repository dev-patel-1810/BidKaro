"""Carry a human's captcha answer into the portal's own form, exactly once.

The answer crosses this module in memory and goes straight into the page. It is
never written to Postgres, a log line, a metric or a file: no column exists for
it and nothing here formats it into a message. A rejected answer is answered by
showing the portal's *new* picture in the app, not by replaying a stored one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.errors import BadRequest, Conflict, ServiceUnavailable
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.discovery import sessions
from app.discovery.browser import BrowserDriverUnavailable, SessionLost
from app.discovery.captcha import parse_captcha_form
from app.discovery.handlers import _capture_url, _save_capture, _save_challenge
from app.models.work import Challenge
from app.services.jobs import JobService

#: The portal's own field allows six characters; a little room is left for
#: portals that use a longer code, but not enough to hold anything else.
MAX_ANSWER_LENGTH = 16


def answer_challenge(
    job_id: uuid.UUID,
    company_id: uuid.UUID,
    answer: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Submit ``answer`` to the parked job's page; return whether it was taken.

    Raises ``Conflict`` when the job is not waiting or its browser is gone.
    The answer is not returned, stored or echoed anywhere.
    """

    settings = settings or get_settings()
    if not settings.discovery_enabled:
        raise ServiceUnavailable("Document fetching is not enabled in this deployment.")

    answer = (answer or "").strip()
    if not answer:
        raise BadRequest("Type the characters shown in the picture.")
    if len(answer) > MAX_ANSWER_LENGTH:
        raise BadRequest("That answer is longer than the portal's field allows.")

    with SessionLocal() as session:
        jobs = JobService(session, settings)
        job = jobs.get_visible(job_id, company_id)
        if job.state != "needs_you":
            raise Conflict("This job is not waiting for an answer.")
        challenge = jobs.challenges.open_for_job(job.id)
        if challenge is None:
            raise Conflict("This job is not waiting for an answer.")
        now = utcnow()
        if challenge.expires_at <= now:
            jobs.expire_challenges()
            raise Conflict("This request expired. Start the fetch again.")

    lane = sessions.lane(job_id)
    if lane is None or lane.lost:
        raise Conflict(
            "The browser session this job was using has ended. Start the fetch "
            "again to open a new one."
        )

    try:
        try:
            lane.call(lambda driver: driver.submit_answer(answer))
        finally:
            # Drop the reference as soon as it has been typed. Nothing below
            # this line needs it, and nothing outside this function sees it.
            answer = None
    except SessionLost as exc:
        raise Conflict(
            "The browser session this job was using has ended. Start the fetch "
            "again to open a new one."
        ) from exc
    except BrowserDriverUnavailable as exc:
        raise Conflict(
            "The portal page no longer shows a challenge. The job will continue "
            "on its own."
        ) from exc

    accepted = _portal_took_it(lane)
    if accepted:
        with SessionLocal() as session:
            JobService(session, settings).signal_challenge(job_id, company_id)
        return True

    # The portal drew a new picture. Keep the same request, refresh the two
    # images, and let the user try the new characters.
    _refresh_picture(settings, job_id, lane)
    return False


def _portal_took_it(lane: sessions.SessionLane) -> bool:
    """Whether the answer produced a file or moved the page on."""

    if lane.call(lambda driver: driver.has_pending_download()):
        return True
    try:
        snapshot = lane.call(lambda driver: driver.snapshot())
    except SessionLost:
        return True
    # Only the document captcha form is a rejection. The portal's ordinary
    # search form can carry a captcha too, and a page after a correct answer
    # (a notice or a download page) may show that search form. Treating any
    # captcha marker as a rejection is what asked the user to answer the same
    # document again after a correct answer had already moved the page on.
    form = parse_captcha_form(snapshot.html or "") if snapshot is not None else None
    if form is not None:
        return False
    text = ((snapshot.text if snapshot is not None else "") or "").casefold()
    if "invalid captcha" in text or "answer was not accepted" in text:
        return False
    return True


def _refresh_picture(
    settings: Settings, job_id: uuid.UUID, lane: sessions.SessionLane
) -> None:
    """Replace the parked challenge's pictures with the portal's new ones."""

    try:
        snapshot = lane.call(lambda driver: driver.snapshot())
        screenshot = lane.call(lambda driver: driver.screenshot())
    except SessionLost:
        return
    form = parse_captcha_form(snapshot.html or "") if snapshot is not None else None
    image = form.image if form is not None else None
    image_path = _save_challenge(settings, job_id, image or screenshot)
    screen_path = _save_capture(settings, job_id, screenshot, "screen")

    with SessionLocal() as session:
        challenge = session.scalars(
            select(Challenge)
            .where(Challenge.job_id == job_id, Challenge.state == "open")
            .order_by(Challenge.opened_at.desc())
        ).first()
        if challenge is None:
            return
        if image_path:
            challenge.image_path = image_path
        context = dict(challenge.context or {})
        context["answer_rejected"] = True
        context["instructions"] = (
            "That answer was not accepted. The portal has drawn a new picture; "
            "type the characters in the new picture and press Submit. BidKaro "
            "carries the answer into the portal's form once and never stores it."
        )
        if screen_path:
            context["screenshot_url"] = _capture_url(job_id, screen_path)
        challenge.context = context
        session.commit()
