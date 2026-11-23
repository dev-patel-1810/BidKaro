from __future__ import annotations

import uuid
from datetime import timedelta

from app.core.db import SessionLocal
from app.core.time import utcnow
from app.models.corpus import Tender
from app.models.workflow import Reminder
from app.repositories.assessment import CompanyTenderRepository
from app.services.builtin_handlers import register_builtin_handlers
from app.services.reminders import ReminderService
from app.services.runner import JobRunner
from app.services.workflow import WorkflowService
from tests.conftest import SAMPLE_PAGES, make_pdf
from tests.helpers import auth_header, verify


def _create_tender(
    client,
    headers,
    *,
    title="Construction of road",
    reference="NHAI/2026/1",
    authority="NHAI",
    published_at=None,
    pages=None,
):
    data = {
        "work_title": title,
        "authority": authority,
        "reference": reference,
        "location": "Maharashtra",
        "region": "Maharashtra",
        "closing_at": "2026-12-31T12:00:00+00:00",
        "source_url": "https://example.test/tender/1",
    }
    if published_at is not None:
        data["published_at"] = published_at
    response = client.post(
        "/api/tenders",
        headers=headers,
        files={
            "file": (
                "tender.pdf",
                make_pdf(pages or SAMPLE_PAGES),
                "application/pdf",
            )
        },
        data=data,
    )
    assert response.status_code == 201, response.text
    tender = response.json()
    documents = client.get(
        f"/api/tenders/{tender['id']}/documents", headers=headers
    )
    assert documents.status_code == 200, documents.text
    return tender, documents.json()["items"][0]["id"]


def _create_requirement(
    client,
    headers,
    *,
    tender_id,
    document_id,
    title="Average annual turnover",
    excerpt=(
        "The bidder shall have an average annual turnover of at least INR 5 crore "
        "during the last three financial years."
    ),
):
    response = client.post(
        f"/api/tenders/{tender_id}/requirements",
        headers=headers,
        json={
            "document_id": document_id,
            "page_index": 1,
            "excerpt": excerpt,
            "category": "eligibility",
            "phase": "pre_award",
            "title": title,
            "obligation": "Submit audited financial statements.",
            "mandatory_state": "mandatory",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _save_tender(company_id: str, tender_id: str) -> None:
    with SessionLocal() as session:
        CompanyTenderRepository(session).upsert(
            company_id=uuid.UUID(company_id),
            tender_id=uuid.UUID(tender_id),
            values={"saved_at": utcnow(), "dismissed_at": None},
        )
        session.commit()


def _capture(tender_id: str):
    with SessionLocal() as session:
        snapshot, created, changes = WorkflowService(session).capture_snapshot(
            uuid.UUID(tender_id)
        )
        return snapshot, created, changes


def _set_closing(tender_id: str, closing_at) -> None:
    with SessionLocal() as session:
        tender = session.get(Tender, uuid.UUID(tender_id))
        assert tender is not None
        tender.closing_at = closing_at
        session.commit()


def test_snapshot_is_ordered_idempotent_and_diffs_fields(client, fake_email):
    owner = verify(client, fake_email, email="snapshot@example.com")
    headers = auth_header(owner["access_token"])
    tender, _ = _create_tender(client, headers)

    accepted = client.post(
        f"/api/tenders/{tender['id']}/snapshot", headers=headers
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["job_id"]

    register_builtin_handlers()
    assert JobRunner().run_once() is True

    snapshots = client.get(
        f"/api/tenders/{tender['id']}/snapshots", headers=headers
    )
    assert snapshots.status_code == 200, snapshots.text
    assert snapshots.json()["total"] == 1

    _set_closing(tender["id"], utcnow() + timedelta(days=40))
    second, created, changes = _capture(tender["id"])
    assert created is True
    assert any(change.field_key == "closing_at" for change in changes)

    same, created_again, no_changes = _capture(tender["id"])
    assert created_again is False
    assert same.id == second.id
    assert no_changes == []

    history = client.get(
        f"/api/tenders/{tender['id']}/snapshots", headers=headers
    ).json()
    assert history["total"] == 2
    newest, oldest = history["items"]
    assert newest["captured_at"] > oldest["captured_at"]
    assert newest["requirement_count"] == 0

    feed = client.get(
        f"/api/tenders/{tender['id']}/changes", headers=headers
    ).json()
    assert feed["total"] >= 1
    closing = next(
        item for item in feed["items"] if item["field_key"] == "closing_at"
    )
    assert closing["change_type"] == "field_changed"
    assert closing["old_value"] != closing["new_value"]


def test_requirement_changes_exclude_human_review_state(client, fake_email):
    owner = verify(client, fake_email, email="req-snapshot@example.com")
    headers = auth_header(owner["access_token"])
    tender, document_id = _create_tender(client, headers)
    requirement = _create_requirement(
        client, headers, tender_id=tender["id"], document_id=document_id
    )

    _capture(tender["id"])
    reviewed = client.patch(
        f"/api/requirements/{requirement['id']}",
        headers=headers,
        json={"state": "needs_review", "note": "Check the scan."},
    )
    assert reviewed.status_code == 200, reviewed.text
    _, created, changes = _capture(tender["id"])
    assert created is False
    assert changes == []

    second = _create_requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        title="Bid security",
        excerpt=(
            "A bid security of INR 2,00,000 must be submitted with the bid."
        ),
    )
    _, created, changes = _capture(tender["id"])
    assert created is True
    added = [item for item in changes if item.change_type == "requirement_added"]
    assert len(added) == 1
    assert added[0].field_key == f"req:{second['id']}"
    assert added[0].new_value == "Bid security"


def test_change_feed_is_company_scoped_to_saved_tenders(client, fake_email):
    first = verify(client, fake_email, email="feed-one@example.com")
    first_headers = auth_header(first["access_token"])
    tender, _ = _create_tender(client, first_headers)
    _capture(tender["id"])
    _set_closing(tender["id"], utcnow() + timedelta(days=20))
    _, _, changes = _capture(tender["id"])
    assert changes
    change_id = str(changes[0].id)

    second = verify(
        client,
        fake_email,
        email="feed-two@example.com",
        company_name="Other Infra",
    )
    second_headers = auth_header(second["access_token"])

    assert client.get("/api/changes", headers=second_headers).json()["total"] == 0

    _save_tender(first["company"]["id"], tender["id"])
    feed = client.get("/api/changes", headers=first_headers)
    assert feed.status_code == 200, feed.text
    assert feed.json()["total"] >= 1
    assert any(item["id"] == change_id for item in feed.json()["items"])

    reviewed = client.patch(
        f"/api/changes/{change_id}",
        headers=first_headers,
        json={"review_state": "acknowledged"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_state"] == "acknowledged"
    assert reviewed.json()["reviewed_by"] == first["user"]["id"]

    pending = client.get(
        "/api/changes", headers=first_headers, params={"review_state": "pending"}
    ).json()
    assert all(item["id"] != change_id for item in pending["items"])
    acknowledged = client.get(
        "/api/changes",
        headers=first_headers,
        params={"review_state": "acknowledged"},
    ).json()
    assert any(item["id"] == change_id for item in acknowledged["items"])


def test_reminders_reopen_on_change_and_task_completion_removes_them(
    client, fake_email
):
    owner = verify(client, fake_email, email="reminders@example.com")
    headers = auth_header(owner["access_token"])
    company_id = owner["company"]["id"]
    tender, _ = _create_tender(client, headers)
    _save_tender(company_id, tender["id"])

    settings = client.patch(
        "/api/company",
        headers=headers,
        json={"reminder_opt_in": True, "reminder_lead_days": [7, 3, 1]},
    )
    assert settings.status_code == 200, settings.text

    listed = client.get("/api/reminders", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["email_enabled"] is True
    deadline_items = [item for item in body["items"] if item["type"] == "deadline"]
    assert len(deadline_items) == 3
    original_due = {item["key"]: item["due_at"] for item in deadline_items}

    first = deadline_items[0]
    acknowledged = client.post(
        f"/api/reminders/{first['id']}/acknowledge", headers=headers
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["acknowledged_at"] is not None

    _set_closing(tender["id"], utcnow() + timedelta(days=60))
    moved = client.get("/api/reminders", headers=headers).json()["items"]
    moved_item = next(item for item in moved if item["key"] == first["key"])
    assert moved_item["due_at"] != original_due[first["key"]]
    assert moved_item["acknowledged_at"] is None
    assert moved_item["state"] == "open"

    dismissed = client.post(
        f"/api/reminders/{moved_item['id']}/dismiss", headers=headers
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["state"] == "dismissed"

    task = client.post(
        "/api/checklist",
        headers=headers,
        json={
            "tender_id": tender["id"],
            "title": "Upload bid security",
            "due_at": "2026-12-10T10:00:00+00:00",
            "evidence_check": "Bank guarantee in the bidder's name.",
        },
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    with_tasks = client.get("/api/reminders", headers=headers).json()["items"]
    assert any(item["task_id"] == task_id for item in with_tasks)

    done = client.patch(
        f"/api/checklist/{task_id}",
        headers=headers,
        json={"status": "done"},
    )
    assert done.status_code == 200, done.text
    after_done = client.get("/api/reminders", headers=headers).json()["items"]
    assert all(item["task_id"] != task_id for item in after_done)


def test_calendar_export_is_authenticated_and_cited(client, fake_email):
    owner = verify(client, fake_email, email="calendar@example.com")
    headers = auth_header(owner["access_token"])
    tender, document_id = _create_tender(client, headers)
    _save_tender(owner["company"]["id"], tender["id"])
    task = client.post(
        "/api/checklist",
        headers=headers,
        json={
            "tender_id": tender["id"],
            "title": "Upload, signed bid",
            "due_at": "2026-11-30T10:00:00+00:00",
            "document_id": document_id,
            "page_index": 2,
            "clause_ref": "Clause 5",
            "excerpt": "Bids must be submitted before the deadline.",
            "source_url": "https://example.test/tender/1",
        },
    )
    assert task.status_code == 201, task.text

    unauthenticated = client.get(f"/api/calendar/tender/{tender['id']}.ics")
    assert unauthenticated.status_code == 401

    tender_ics = client.get(
        f"/api/calendar/tender/{tender['id']}.ics", headers=headers
    )
    assert tender_ics.status_code == 200, tender_ics.text
    assert tender_ics.headers["content-type"].startswith("text/calendar")
    assert tender_ics.text.startswith("BEGIN:VCALENDAR")
    assert "Tender deadline: Construction of road" in tender_ics.text
    assert "Upload\\, signed bid" in tender_ics.text
    assert "Clause 5" in tender_ics.text
    assert tender_ics.text.endswith("END:VCALENDAR\r\n")

    tasks_ics = client.get("/api/calendar/tasks.ics", headers=headers)
    assert tasks_ics.status_code == 200, tasks_ics.text
    assert "Tender deadline" in tasks_ics.text
    assert "Upload\\, signed bid" in tasks_ics.text


def test_corrigendum_is_proposed_then_human_confirmed(client, fake_email):
    owner = verify(client, fake_email, email="lineage@example.com")
    headers = auth_header(owner["access_token"])
    original, _ = _create_tender(
        client,
        headers,
        title="Construction of highway package 7",
        reference="NHAI/2026/77",
        published_at="2026-01-10T12:00:00+00:00",
        pages=[SAMPLE_PAGES[0]],
    )
    amendment, _ = _create_tender(
        client,
        headers,
        title="Construction of highway package 7",
        reference="NHAI/2026/77",
        published_at="2026-02-10T12:00:00+00:00",
        pages=[SAMPLE_PAGES[1]],
    )
    assert amendment["supersedes_tender_id"] == original["id"]
    assert amendment["supersedes_state"] == "proposed"

    confirmed = client.post(
        f"/api/tenders/{amendment['id']}/lineage/{original['id']}/confirm",
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["supersedes_state"] == "confirmed"

    changes = client.get(
        f"/api/tenders/{amendment['id']}/changes", headers=headers
    ).json()
    lineage = [
        item for item in changes["items"] if item["change_type"] == "lineage_linked"
    ]
    assert lineage
    assert any(
        item["field_key"] == "supersedes_state"
        and item["new_value"] == "confirmed"
        for item in lineage
    )

    rejected = client.post(
        f"/api/tenders/{amendment['id']}/lineage/{original['id']}/reject",
        headers=headers,
    )
    assert rejected.status_code == 409, rejected.text


def test_due_reminder_email_is_sent_once(client, fake_email):
    owner = verify(client, fake_email, email="mail-reminder@example.com")
    headers = auth_header(owner["access_token"])
    tender, _ = _create_tender(client, headers)
    _save_tender(owner["company"]["id"], tender["id"])
    settings = client.patch(
        "/api/company",
        headers=headers,
        json={"reminder_opt_in": True, "reminder_lead_days": [1]},
    )
    assert settings.status_code == 200, settings.text
    _set_closing(tender["id"], utcnow() + timedelta(hours=2))

    with SessionLocal() as session:
        first = ReminderService(session, email=fake_email).send_due(
            dry_run=False, email=fake_email
        )
    assert len(first["sent"]) == 1, first
    assert len(fake_email.reminders) == 1
    reminder_id = uuid.UUID(first["sent"][0]["reminder_id"])
    with SessionLocal() as session:
        row = session.get(Reminder, reminder_id)
        assert row is not None
        assert row.state == "sent"
        assert row.sent_at is not None

    with SessionLocal() as session:
        second = ReminderService(session, email=fake_email).send_due(
            dry_run=False, email=fake_email
        )
    assert second["sent"] == []
    assert len(fake_email.reminders) == 1

