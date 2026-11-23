from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import utcnow
from app.models.work import Challenge, Job, LaneLease
from app.repositories.work import JobRepository
from app.services.job_registry import register_handler
from app.services.job_types import JobResult
from app.services.jobs import JobService
from app.services.runner import JobRunner
from app.services.scheduler import Scheduler
from tests.helpers import auth_header, verify


def _company_id(verified: dict) -> uuid.UUID:
    return uuid.UUID(verified["company"]["id"])


def _enqueue(company_id: uuid.UUID | None = None, **kwargs):
    with SessionLocal() as session:
        job, created = JobService(session).enqueue(company_id=company_id, **kwargs)
        return job.id, created


def _job(job_id: uuid.UUID) -> Job:
    with SessionLocal() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        return job


def _lane(name: str = "browser") -> LaneLease | None:
    with SessionLocal() as session:
        return session.get(LaneLease, name)


def test_enqueue_is_idempotent():
    first, created = _enqueue(
        kind="snapshot_tender", idempotency_key="same-key", payload={"tender": "x"}
    )
    second, created_again = _enqueue(
        kind="snapshot_tender", idempotency_key="same-key", payload={"tender": "y"}
    )
    assert created is True
    assert created_again is False
    assert first == second
    job = _job(first)
    assert job.payload == {"tender": "x"}


def test_runner_completes_a_job_and_marks_the_global_run():
    register_handler(
        "scrape_portal",
        lambda _context: JobResult(metrics={"listed": 2}, notes="read two notices"),
    )
    job_id, _ = _enqueue(kind="scrape_portal", lane="browser", payload={"trigger": "test"})

    assert JobRunner().run_once() is True

    job = _job(job_id)
    assert job.state == "done"
    assert job.attempts == 1
    assert job.lease_owner is None
    assert job.finished_at is not None
    assert _lane() is None

    with SessionLocal() as session:
        from app.repositories.work import RunRepository

        runs = RunRepository(session).list_for_job(job_id)
    assert len(runs) == 1
    assert runs[0].status == "done"
    assert runs[0].metrics == {"listed": 2}

    with SessionLocal() as session:
        assert JobService(session).last_completed_run_at() is not None


def test_a_failed_job_is_recorded_and_can_be_retried(client, fake_email):
    owner = verify(client, fake_email, email="retry@example.com")
    company_id = _company_id(owner)

    def explode(_context):
        raise RuntimeError("portal refused the page")

    register_handler("snapshot_tender", explode)
    with SessionLocal() as session:
        job, _ = JobRepository(session).enqueue(
            kind="snapshot_tender",
            company_id=company_id,
            max_attempts=1,
        )
        session.commit()
        job_id = job.id

    assert JobRunner().run_once() is True
    job = _job(job_id)
    assert job.state == "failed"
    assert job.last_error == "portal refused the page"

    with SessionLocal() as session:
        retried = JobService(session).retry(job_id, company_id)
    assert retried.state == "queued"
    assert retried.attempts == 0
    assert retried.last_error is None


def test_the_browser_lane_is_exclusive_and_resumes_the_same_job(client, fake_email):
    owner = verify(client, fake_email, email="lane@example.com")
    company_id = _company_id(owner)

    runs: dict = {}

    def handler(context):
        count = runs.get(context.job_id, 0) + 1
        runs[context.job_id] = count
        # The first attempt parks; the resume after the answer completes. An
        # answered challenge restarts the attempt budget, so the resume is
        # claim number one again and cannot be recognised by ``attempts``.
        if context.payload.get("park") and count == 1:
            context.park(
                kind="captcha",
                context={"url": "https://example.test/notice"},
                image_path="challenge.png",
            )
        return JobResult()

    register_handler("fetch_documents", handler)
    first, _ = _enqueue(
        company_id=company_id,
        kind="fetch_documents",
        lane="browser",
        payload={"park": True},
    )
    second, _ = _enqueue(
        company_id=company_id,
        kind="fetch_documents",
        lane="browser",
        payload={"park": True},
    )

    runner = JobRunner()
    assert runner.run_once() is True
    assert _job(first).state == "needs_you"
    lane = _lane()
    assert lane is not None
    assert lane.owner_id == first
    assert lane.reserved_for_challenge is True

    # The lane is reserved for the first job, so the second is left queued.
    assert runner.run_once() is False
    assert _job(second).state == "queued"

    with SessionLocal() as session:
        JobService(session).signal_challenge(first, company_id)
    assert _job(first).state == "queued"

    # The same job owns the lane and may claim it again; its second attempt
    # completes and releases the lane.
    assert runner.run_once() is True
    assert _job(first).state == "done"
    assert _lane() is None

    assert runner.run_once() is True
    assert _job(second).state == "needs_you"


def test_an_expired_challenge_records_partial_and_releases_the_lane(
    client, fake_email
):
    owner = verify(client, fake_email, email="expire@example.com")
    company_id = _company_id(owner)

    def handler(context):
        context.park(kind="captcha", context={}, image_path="challenge.png")
        return JobResult()

    register_handler("fetch_documents", handler)
    job_id, _ = _enqueue(
        company_id=company_id, kind="fetch_documents", lane="browser"
    )
    assert JobRunner().run_once() is True
    assert _job(job_id).state == "needs_you"

    with SessionLocal() as session:
        challenge = session.scalar(
            select(Challenge).where(Challenge.job_id == job_id)
        )
        assert challenge is not None
        challenge.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    with SessionLocal() as session:
        assert JobService(session).expire_challenges() == 1

    job = _job(job_id)
    assert job.state == "partial"
    assert job.last_error == "file request expired"
    assert _lane() is None
    with SessionLocal() as session:
        challenge = session.scalar(
            select(Challenge).where(Challenge.job_id == job_id)
        )
        assert challenge is not None
        assert challenge.state == "expired"


def test_scheduler_catches_up_once_and_does_not_duplicate_a_pending_run():
    register_handler("scrape_portal", lambda _context: JobResult())
    with SessionLocal() as session:
        JobService(session).state.set_datetime(
            "last_completed_run_at", utcnow() - timedelta(days=3)
        )
        session.commit()

    scheduler = Scheduler()
    assert scheduler.tick() is True
    with SessionLocal() as session:
        jobs = JobRepository(session).list(kind="scrape_portal")
    assert len(jobs) == 1
    assert jobs[0].payload == {"trigger": "scheduled"}

    assert scheduler.tick() is False
    with SessionLocal() as session:
        assert len(JobRepository(session).list(kind="scrape_portal")) == 1

    # A recent completion makes the next tick a no-op.
    assert JobRunner().run_once() is True
    assert scheduler.tick() is False


def test_stale_worker_leases_are_recovered():
    job_id, _ = _enqueue(kind="fetch_documents", lane="browser")
    with SessionLocal() as session:
        claimed = JobRepository(session).claim_next(
            worker_id="dead-worker", lease_seconds=10
        )
        assert claimed is not None
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    with SessionLocal() as session:
        assert JobService(session).recover_stale() == 1

    job = _job(job_id)
    assert job.state == "queued"
    assert job.last_error == "Worker stopped before the job finished."
    assert _lane() is None


def test_job_routes_are_authenticated_and_company_scoped(client, fake_email):
    owner = verify(client, fake_email, email="jobs-owner@example.com")
    other = verify(client, fake_email, email="jobs-other@example.com")
    owner_headers = auth_header(owner["access_token"])
    other_headers = auth_header(other["access_token"])
    owner_company = _company_id(owner)

    job_id, _ = _enqueue(
        company_id=owner_company,
        kind="snapshot_tender",
        payload={"tender_id": "t-1"},
    )

    assert client.get("/api/jobs").status_code == 401
    owner_read = client.get(f"/api/jobs/{job_id}", headers=owner_headers)
    assert owner_read.status_code == 200
    assert owner_read.json()["kind"] == "snapshot_tender"
    assert client.get(f"/api/jobs/{job_id}", headers=other_headers).status_code == 404

    owner_list = client.get("/api/jobs", headers=owner_headers)
    assert owner_list.status_code == 200
    assert [item["id"] for item in owner_list.json()["items"]] == [str(job_id)]
    other_list = client.get("/api/jobs", headers=other_headers)
    assert other_list.status_code == 200
    assert other_list.json()["items"] == []

    assert (
        client.post(f"/api/jobs/{job_id}/cancel", headers=other_headers).status_code
        == 404
    )
    assert (
        client.post(f"/api/jobs/{job_id}/challenge", headers=owner_headers).status_code
        == 409
    )
    assert client.get("/api/jobs/needs-you", headers=owner_headers).json()["items"] == []

    cancelled = client.post(f"/api/jobs/{job_id}/cancel", headers=owner_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert (
        client.post(f"/api/jobs/{job_id}/cancel", headers=owner_headers).status_code
        == 409
    )
