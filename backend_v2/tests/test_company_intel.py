from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models.work import Job
from app.repositories.corpus import TenderRepository
from app.services.builtin_handlers import handle_extract_company_document
from app.services.company_intel import harvest_terms
from app.services.job_registry import register_handler
from app.services.runner import JobRunner
from tests.conftest import make_pdf
from tests.helpers import auth_header, verify

#: Relative to the real clock: the default list hides a closed tender, so a
#: fixed date would make these fixtures look closed as time passes.
NOW = datetime.now(timezone.utc)

PROFILE_PAGES = [
    (
        "Acme Infra provides document scanning and document digitization.\n"
        "We also provide document digitization for court records and land records.\n"
        "Document management system, data entry and metadata indexing are part of our work."
    ),
    (
        "Our document scanning team converts physical files into digital records.\n"
        "Document digitization is used for judicial records and examination processing.\n"
        "Data entry and metadata indexing support every document management system.\n"
        "Empanelment with NICSI for scanning and digitization of records.\n"
        "CIN: U74899DL1994PTC060000\n"
        "Email: sales@acme.example"
    ),
]


def _tender(
    *,
    title: str,
    location: str = "Delhi",
    created_minutes_ago: int = 0,
) -> str:
    with SessionLocal() as session:
        tender = TenderRepository(session).create(
            {
                "dedupe_key": f"test:{uuid.uuid4()}",
                "portal_tender_id": f"2027_T_{uuid.uuid4().hex[:8]}_1",
                "work_title": title,
                "location": location,
                "region": location,
                "closing_at": NOW + timedelta(days=30),
                "source_type": "official_portal",
                "extracted_from": "notice_page",
                "created_at": NOW + timedelta(minutes=created_minutes_ago),
            }
        )
        session.commit()
        return str(tender.id)


def _owner(client, fake_email, *, email: str = "intel@example.com") -> dict:
    owner = verify(client, fake_email, email=email, company_name="Acme Infra")
    headers = auth_header(owner["access_token"])
    # A region alone is not a work profile; the uploaded document supplies the
    # work and makes the profile complete.
    profile = client.patch(
        "/api/company", headers=headers, json={"region": "Delhi"}
    )
    assert profile.status_code == 200, profile.text
    return headers


def _upload_profile(client, headers) -> dict:
    response = client.post(
        "/api/company/documents",
        headers=headers,
        files={
            "file": (
                "about-acme.pdf",
                make_pdf(PROFILE_PAGES),
                "application/pdf",
            )
        },
        data={"document_type": "about_company", "title": "About Acme Infra"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run_intel_job() -> None:
    register_handler(
        "extract_company_document", handle_extract_company_document
    )
    assert JobRunner().run_once() is True


def _job_count() -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count(Job.id))) or 0)


def test_harvest_reads_repeated_phrases_and_never_a_client_or_identifier():
    terms = harvest_terms(
        [
            (index + 1, str(index + 1), text)
            for index, text in enumerate(PROFILE_PAGES)
        ],
        strip_phrases=("Acme Infra",),
    )
    by_text = {item.term: item for item in terms}

    assert "document digitization" in by_text
    assert "document scanning" in by_text
    assert "document management system" in by_text
    assert "metadata indexing" in by_text
    # The client and the identifiers are not work evidence.
    assert not any("nicsi" in term for term in by_text)
    assert not any("cin" in term for term in by_text)
    assert not any("acme" in term for term in by_text)

    phrase = by_text["document digitization"]
    assert phrase.page_index in {1, 2}
    assert "document digitization" in phrase.excerpt.casefold()


def test_an_about_company_pdf_is_read_into_terms(client, fake_email):
    headers = _owner(client, fake_email)
    document = _upload_profile(client, headers)

    assert document["document_type"] == "about_company"
    assert document["extraction_state"] == "pending"
    assert document["term_count"] == 0
    assert _job_count() == 1

    _run_intel_job()

    listed = client.get("/api/company/documents", headers=headers)
    assert listed.status_code == 200, listed.text
    stored = listed.json()["items"][0]
    assert stored["extraction_state"] == "done"
    assert stored["term_count"] > 0

    terms = client.get(
        f"/api/company/documents/{document['id']}/terms", headers=headers
    )
    assert terms.status_code == 200, terms.text
    body = terms.json()
    assert body["extraction_state"] == "done"
    assert body["items"]
    phrase = next(
        item for item in body["items"] if item["term"] == "document digitization"
    )
    assert phrase["kind"] == "phrase"
    assert phrase["page_index"] >= 1
    assert phrase["printed_label"] is not None
    assert "document digitization" in phrase["excerpt"].casefold()


def test_raw_document_terms_do_not_make_a_profile_or_match_work(client, fake_email):
    """The harvest is a human-facing suggestion, never a fit input.

    An uploaded about-company PDF used to complete the profile by itself,
    which let a project name or a client name become work evidence. Only a
    confirmed capability/experience or a declared term may do that now.
    """

    headers = _owner(client, fake_email)
    _upload_profile(client, headers)
    _run_intel_job()

    digitization = _tender(title="Digitization of court records", created_minutes_ago=1)
    road = _tender(title="Construction of a four lane road", created_minutes_ago=2)

    listed = client.get("/api/tenders", headers=headers)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    # A region alone is still not a work profile, even after extracting a
    # document full of work words.
    assert body["profile_complete"] is False
    assert {item["id"] for item in body["items"]} == {digitization, road}
    for item in body["items"]:
        assert item["relevance"]["tier"] == 7
        assert item["relevance"]["bucket"] == "possible"
        assert item["relevance"]["matched_terms"] == []


def test_raw_document_terms_never_list_a_tender_on_their_own(client, fake_email):
    headers = _owner(client, fake_email)
    _upload_profile(client, headers)
    _run_intel_job()

    outside = _tender(
        title="Digitization and scanning of records", location="Karnataka"
    )

    listed = client.get("/api/tenders", headers=headers)

    assert listed.status_code == 200, listed.text
    assert listed.json()["profile_complete"] is False
    assert [item["id"] for item in listed.json()["items"]] == [outside]
    assert listed.json()["items"][0]["relevance"]["tier"] == 7


def test_removing_a_document_term_changes_only_the_suggestion_list(client, fake_email):
    headers = _owner(client, fake_email)
    document = _upload_profile(client, headers)
    _run_intel_job()
    digitization = _tender(title="Digitization of court records")

    listed = client.get("/api/tenders", headers=headers)
    assert listed.json()["profile_complete"] is False
    assert [item["id"] for item in listed.json()["items"]] == [digitization]

    terms = client.get(
        f"/api/company/documents/{document['id']}/terms", headers=headers
    ).json()["items"]
    term = next(item for item in terms if item["term"] == "digitization")
    removed = client.delete(
        f"/api/company/documents/{document['id']}/terms/{term['id']}",
        headers=headers,
    )
    assert removed.status_code == 204, removed.text

    phrase = next(
        item
        for item in terms
        if item["term"] == "document digitization"
        and item["id"] != term["id"]
    )
    assert (
        client.delete(
            f"/api/company/documents/{document['id']}/terms/{phrase['id']}",
            headers=headers,
        ).status_code
        == 204
    )

    remaining = client.get(
        f"/api/company/documents/{document['id']}/terms", headers=headers
    ).json()["items"]
    assert all(
        item["term"] not in {"digitization", "document digitization"}
        for item in remaining
    )

    # Removing a suggestion does not change fit, because it never fed fit.
    after = client.get("/api/tenders", headers=headers)
    assert after.json()["profile_complete"] is False
    assert [item["id"] for item in after.json()["items"]] == [digitization]
    assert after.json()["items"][0]["relevance"]["tier"] == 7


def test_deleting_an_uploaded_document_leaves_the_declared_profile_alone(
    client, fake_email
):
    headers = _owner(client, fake_email)
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"region": "Delhi", "services": ["digitization"]},
    )
    assert profile.status_code == 200, profile.text
    document = _upload_profile(client, headers)
    _run_intel_job()
    digitization = _tender(title="Digitization of court records")
    road = _tender(title="Construction of a four lane road")

    listed = client.get("/api/tenders", headers=headers)

    assert listed.status_code == 200, listed.text
    assert listed.json()["profile_complete"] is True
    assert [item["id"] for item in listed.json()["items"]] == [digitization]
    assert listed.json()["items"][0]["relevance"]["tier"] == 5

    deleted = client.delete(
        f"/api/company/documents/{document['id']}", headers=headers
    )
    assert deleted.status_code == 204, deleted.text

    after = client.get("/api/tenders", headers=headers)

    # The declared work is still the company's own declaration; the upload was
    # never what made the profile complete.
    assert after.json()["profile_complete"] is True
    assert [item["id"] for item in after.json()["items"]] == [digitization]
    assert road not in [item["id"] for item in after.json()["items"]]


def test_a_profile_can_be_read_again_and_says_pending_meanwhile(client, fake_email):
    headers = _owner(client, fake_email)
    document = _upload_profile(client, headers)
    _run_intel_job()

    first = client.get(
        f"/api/company/documents/{document['id']}/terms", headers=headers
    ).json()
    assert first["extraction_state"] == "done"
    assert first["term_count"] > 0

    response = client.post(
        f"/api/company/documents/{document['id']}/extract", headers=headers
    )
    assert response.status_code == 202, response.text

    # The read is asynchronous: the document says "pending" until the job
    # writes a terminal state, instead of leaving the old "done" visible.
    listed = client.get("/api/company/documents", headers=headers).json()["items"][0]
    assert listed["extraction_state"] == "pending"

    _run_intel_job()

    again = client.get(
        f"/api/company/documents/{document['id']}/terms", headers=headers
    ).json()
    assert again["extraction_state"] == "done"
    assert again["term_count"] == first["term_count"]


def test_only_an_about_company_profile_is_read_for_suggestions(client, fake_email):
    headers = _owner(client, fake_email)
    response = client.post(
        "/api/company/documents",
        headers=headers,
        files={
            "file": (
                "certificate.pdf",
                make_pdf(["Completion certificate for a road project."]),
                "application/pdf",
            )
        },
        data={
            "document_type": "completion_certificate",
            "title": "Road certificate",
        },
    )
    assert response.status_code == 201, response.text
    assert _job_count() == 0
    assert response.json()["extraction_state"] == "pending"
