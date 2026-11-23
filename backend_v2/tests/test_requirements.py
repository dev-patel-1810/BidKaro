from __future__ import annotations

import uuid

from sqlalchemy import select

from app.api.deps import get_ai_provider
from app.core.db import SessionLocal
from app.main import app
from app.models.corpus import DocumentPage, Requirement, TenderDocument
from app.services.job_registry import clear_handlers
from app.services.builtin_handlers import register_builtin_handlers
from app.services.requirement_service import RequirementService
from app.services.runner import JobRunner
from tests.conftest import SAMPLE_PAGES, make_pdf
from tests.helpers import auth_header, verify

EXCERPT_1 = (
    "The bidder shall have an average annual turnover of at least INR 5 crore "
    "during the last three financial years."
)
EXCERPT_2 = (
    "Bids must be submitted before 15 November 2026 at 17:00 IST through the "
    "online portal."
)
NOT_IN_DOCUMENT = (
    "A sentence that is long enough to pass the length gate but appears nowhere "
    "on the cited page."
)
NAVIGATION = "Section 8 - Penalties ..... 12"


class FakeAI:
    model = "fake-model"

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {"requirements": []}
        self.calls = 0
        self.configured = True

    def complete_json(self, *, system: str, user: str) -> dict:
        self.calls += 1
        return self.payload


def _upload_manual(
    client,
    headers,
    *,
    title="Four lane road",
    pages=None,
    filename="tender.pdf",
    data: bytes | None = None,
):
    return client.post(
        "/api/tenders",
        headers=headers,
        files={
            "file": (
                filename,
                data if data is not None else make_pdf(pages or SAMPLE_PAGES),
                "application/pdf",
            )
        },
        data={
            "work_title": title,
            "authority": "NHAI",
            "reference": "NHAI/2026/1",
            "location": "Maharashtra",
            "region": "Maharashtra",
            "closing_at": "2026-12-31T12:00:00+00:00",
        },
    )


def _document_id(tender_id: str) -> uuid.UUID:
    with SessionLocal() as session:
        row = session.scalar(
            select(TenderDocument).where(
                TenderDocument.tender_id == uuid.UUID(tender_id)
            )
        )
        assert row is not None
        return row.id


def test_manual_tender_upload_extracts_pages_and_dedupes(client, fake_email):
    owner = verify(client, fake_email, email="docs@example.com")
    headers = auth_header(owner["access_token"])

    pdf = make_pdf(SAMPLE_PAGES)
    created = _upload_manual(client, headers, data=pdf)
    assert created.status_code == 201, created.text
    tender = created.json()
    tender_id = tender["id"]
    assert tender["document_count"] == 1
    assert tender["work_title"] == "Four lane road"
    assert tender["source_type"] == "manual_upload"

    documents = client.get(f"/api/tenders/{tender_id}/documents", headers=headers)
    assert documents.status_code == 200
    item = documents.json()["items"][0]
    assert item["page_count"] == 3
    assert item["extraction_state"] == "done"
    assert item["has_text_layer"] is True
    assert item["citation_counts"]["total"] == 0
    assert item["is_current"] is True

    pages = client.get(
        f"/api/tender-documents/{item['id']}/pages", headers=headers
    )
    assert pages.status_code == 200
    assert [page["page_index"] for page in pages.json()["items"]] == [1, 2, 3]
    assert pages.json()["items"][1]["printed_label"] == "2"

    page = client.get(
        f"/api/tender-documents/{item['id']}/pages/2", headers=headers
    )
    assert page.status_code == 200
    assert "15 November 2026" in page.json()["text"]
    assert page.json()["is_ocr"] is False

    download = client.get(
        f"/api/tender-documents/{item['id']}/download", headers=headers
    )
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF-")

    # The same bytes and title is the same manual tender, not a second one.
    repeated = _upload_manual(client, headers, data=pdf)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == tender_id
    assert repeated.json()["document_count"] == 1


def test_manual_tender_rejects_a_non_pdf(client, fake_email):
    owner = verify(client, fake_email, email="notpdf@example.com")
    response = client.post(
        "/api/tenders",
        headers=auth_header(owner["access_token"]),
        files={"file": ("notes.txt", b"not a pdf", "text/plain")},
        data={"work_title": "Four lane road"},
    )
    assert response.status_code == 422


def test_document_versions_replace_the_current_file(client, fake_email):
    owner = verify(client, fake_email, email="versions@example.com")
    headers = auth_header(owner["access_token"])
    tender = _upload_manual(client, headers).json()
    tender_id = tender["id"]

    replacement = make_pdf(
        [
            "Section 1 - Revised Eligibility. The bidder shall have an average "
            "annual turnover of at least INR 8 crore during the last three "
            "financial years. This revised document supersedes the earlier one."
        ]
    )
    uploaded = client.post(
        f"/api/tenders/{tender_id}/documents",
        headers=headers,
        files={"file": ("tender.pdf", replacement, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["version"] == "v2"
    assert uploaded.json()["is_current"] is True

    current = client.get(f"/api/tenders/{tender_id}/documents", headers=headers)
    assert len(current.json()["items"]) == 1
    assert current.json()["items"][0]["version"] == "v2"
    assert current.json()["fetch_state"]["documents_present"] == 1

    all_versions = client.get(
        f"/api/tenders/{tender_id}/documents?include_versions=true",
        headers=headers,
    )
    assert [item["version"] for item in all_versions.json()["items"]] == ["v1", "v2"]
    assert all_versions.json()["items"][0]["is_current"] is False
    assert all_versions.json()["items"][1]["supersedes_document_id"] is not None


def _create_tender_and_document(client, headers):
    response = _upload_manual(client, headers)
    assert response.status_code == 201, response.text
    tender_id = response.json()["id"]
    return tender_id, _document_id(tender_id)


def test_extraction_gate_separates_verified_ocr_and_unverified(client, fake_email):
    owner = verify(client, fake_email, email="gate@example.com")
    headers = auth_header(owner["access_token"])
    tender_id, document_id = _create_tender_and_document(client, headers)

    # Page 2 is an OCR page: a match there must start as needs_review.
    with SessionLocal() as session:
        page = session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document_id,
                DocumentPage.page_index == 2,
            )
        )
        assert page is not None
        page.is_ocr = True
        session.commit()

    fake = FakeAI(
        {
            "requirements": [
                {
                    "category": "eligibility",
                    "title": "Minimum turnover",
                    "obligation": "The bidder must show at least INR 5 crore turnover.",
                    "excerpt": EXCERPT_1,
                    "page_index": 1,
                    "mandatory_state": "mandatory",
                    "phase": "pre_award",
                },
                {
                    "category": "submission",
                    "title": "Bid deadline",
                    "obligation": "Submit the bid before the stated deadline.",
                    "excerpt": EXCERPT_2,
                    "page_index": 2,
                    "mandatory_state": "mandatory",
                    "deadline_text": "2026-11-15",
                    "deadline_at": "2026-11-15",
                },
                {
                    "category": "compliance",
                    "title": "Invented claim",
                    "obligation": "An obligation the page never states.",
                    "excerpt": NOT_IN_DOCUMENT,
                    "page_index": 1,
                    "mandatory_state": "unknown",
                },
                {
                    "category": "eligibility",
                    "title": "Duplicate",
                    "obligation": "The same excerpt again.",
                    "excerpt": EXCERPT_1,
                    "page_index": 1,
                    "mandatory": "yes",
                },
                {
                    "category": "compliance",
                    "title": "Contents row",
                    "obligation": "Table of contents entry.",
                    "excerpt": NAVIGATION,
                    "page_index": 3,
                    "mandatory_state": "unknown",
                },
                {
                    "category": "eligibility",
                    "title": "Malformed page",
                    "excerpt": "A long enough excerpt but a page number of zero.",
                    "page_index": 0,
                },
            ]
        }
    )
    with SessionLocal() as session:
        result = RequirementService(session, ai=fake).extract(
            document_id=document_id,
            company_id=uuid.UUID(owner["company"]["id"]),
            actor_user_id=uuid.UUID(owner["user"]["id"]),
        )
    assert fake.calls == 1
    assert result.status == "completed"
    assert result.stats.created == 4
    assert result.stats.duplicates == 1
    assert result.stats.invalid == 1
    assert result.stats.verified == 1
    assert result.stats.needs_review == 1
    assert result.stats.unverified == 2

    listed = client.get(
        f"/api/tenders/{tender_id}/requirements", headers=headers
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 3  # navigation hidden by default
    assert all(not item["is_navigation"] for item in listed.json()["items"])

    with_navigation = client.get(
        f"/api/tenders/{tender_id}/requirements?include_navigation=true",
        headers=headers,
    )
    assert with_navigation.json()["total"] == 4

    needs_review = client.get(
        f"/api/tenders/{tender_id}/requirements?state=needs_review",
        headers=headers,
    ).json()["items"][0]
    assert needs_review["deadline_at"] is not None
    assert needs_review["deadline_at"].startswith("2026-11-15")

    accepted = client.patch(
        f"/api/requirements/{needs_review['id']}",
        headers=headers,
        json={"state": "accepted_ocr", "note": "Checked against the page image."},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "accepted_ocr"

    verified = client.get(
        f"/api/tenders/{tender_id}/requirements?state=verified", headers=headers
    ).json()["items"][0]
    assert (
        client.patch(
            f"/api/requirements/{verified['id']}",
            headers=headers,
            json={"state": "accepted_ocr"},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/requirements/{verified['id']}",
            headers=headers,
            json={"state": "verified"},
        ).status_code
        == 422
    )
    rejected = client.patch(
        f"/api/requirements/{verified['id']}",
        headers=headers,
        json={"state": "rejected", "note": "This clause is a contents row."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"

    summary = client.get(
        f"/api/tenders/{tender_id}/requirements/summary", headers=headers
    )
    assert summary.status_code == 200
    assert summary.json()["total"] == 4
    assert summary.json()["needs_review"] == 0
    assert summary.json()["accepted_ocr"] == 1
    assert summary.json()["rejected"] == 1


def test_company_flags_hide_only_for_that_company(client, fake_email):
    owner = verify(client, fake_email, email="flag-owner@example.com")
    other = verify(client, fake_email, email="flag-other@example.com")
    owner_headers = auth_header(owner["access_token"])
    other_headers = auth_header(other["access_token"])
    tender_id, document_id = _create_tender_and_document(client, owner_headers)

    with SessionLocal() as session:
        RequirementService(
            session,
            ai=FakeAI(
                {
                    "requirements": [
                        {
                            "category": "eligibility",
                            "title": "Minimum turnover",
                            "obligation": "The bidder must show at least INR 5 crore turnover.",
                            "excerpt": EXCERPT_1,
                            "page_index": 1,
                            "mandatory_state": "mandatory",
                        }
                    ]
                }
            ),
        ).extract(
            document_id=document_id,
            company_id=uuid.UUID(owner["company"]["id"]),
            actor_user_id=uuid.UUID(owner["user"]["id"]),
        )

    requirement = client.get(
        f"/api/tenders/{tender_id}/requirements", headers=owner_headers
    ).json()["items"][0]
    requirement_id = requirement["id"]

    flagged = client.post(
        f"/api/requirements/{requirement_id}/flag",
        headers=owner_headers,
        json={"note": "Not relevant to us."},
    )
    assert flagged.status_code == 200, flagged.text
    assert flagged.json()["flagged"] is True
    assert flagged.json()["flag_note"] == "Not relevant to us."

    hidden = client.get(
        f"/api/tenders/{tender_id}/requirements", headers=owner_headers
    )
    assert hidden.json()["total"] == 0
    shown = client.get(
        f"/api/tenders/{tender_id}/requirements?include_ignored=true",
        headers=owner_headers,
    )
    assert shown.json()["total"] == 1

    other_view = client.get(
        f"/api/requirements/{requirement_id}", headers=other_headers
    )
    assert other_view.status_code == 200
    assert other_view.json()["flagged"] is False

    unflag = client.patch(
        f"/api/requirements/{requirement_id}/flag",
        headers=owner_headers,
        json={"ignored": False, "note": None},
    )
    assert unflag.status_code == 200
    assert unflag.json()["flagged"] is False
    visible = client.get(
        f"/api/tenders/{tender_id}/requirements", headers=owner_headers
    )
    assert visible.json()["total"] == 1


def test_human_requirement_is_still_citation_checked(client, fake_email):
    owner = verify(client, fake_email, email="human@example.com")
    headers = auth_header(owner["access_token"])
    tender_id, document_id = _create_tender_and_document(client, headers)

    created = client.post(
        f"/api/tenders/{tender_id}/requirements",
        headers=headers,
        json={
            "document_id": str(document_id),
            "page_index": 1,
            "excerpt": EXCERPT_1,
            "category": "eligibility",
            "title": "Turnover, entered by a reviewer",
            "obligation": "At least INR 5 crore average annual turnover.",
            "mandatory_state": "mandatory",
            "deadline_text": "2026-11-15",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["origin"] == "human"
    assert body["state"] == "verified"
    assert body["deadline_at"].startswith("2026-11-15")

    duplicate = client.post(
        f"/api/tenders/{tender_id}/requirements",
        headers=headers,
        json={
            "document_id": str(document_id),
            "page_index": 1,
            "excerpt": EXCERPT_1,
            "category": "eligibility",
        },
    )
    assert duplicate.status_code == 409

    invented = client.post(
        f"/api/tenders/{tender_id}/requirements",
        headers=headers,
        json={
            "document_id": str(document_id),
            "page_index": 1,
            "excerpt": NOT_IN_DOCUMENT,
            "category": "compliance",
        },
    )
    assert invented.status_code == 201
    assert invented.json()["state"] == "unverified"
    assert invented.json()["state_reason"] == (
        "The excerpt was not found verbatim on the cited page."
    )

    bad_page = client.post(
        f"/api/tenders/{tender_id}/requirements",
        headers=headers,
        json={
            "document_id": str(document_id),
            "page_index": 99,
            "excerpt": EXCERPT_1,
            "category": "eligibility",
        },
    )
    assert bad_page.status_code == 422


def test_extract_route_enqueues_a_job_and_the_handler_writes_requirements(
    client, fake_email, monkeypatch
):
    owner = verify(client, fake_email, email="jobextract@example.com")
    headers = auth_header(owner["access_token"])
    tender_id, document_id = _create_tender_and_document(client, headers)

    fake = FakeAI(
        {
            "requirements": [
                {
                    "category": "eligibility",
                    "title": "Minimum turnover",
                    "obligation": "The bidder must show at least INR 5 crore turnover.",
                    "excerpt": EXCERPT_1,
                    "page_index": 1,
                    "mandatory_state": "mandatory",
                }
            ]
        }
    )
    app.dependency_overrides[get_ai_provider] = lambda: fake
    monkeypatch.setattr(
        "app.services.builtin_handlers.build_ai_provider", lambda: fake
    )
    clear_handlers()
    register_builtin_handlers()

    accepted = client.post(
        f"/api/tender-documents/{document_id}/extract", headers=headers
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    assert JobRunner().run_once() is True
    job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert job["state"] == "done"
    assert job["kind"] == "extract_requirements"

    requirements = client.get(
        f"/api/tenders/{tender_id}/requirements", headers=headers
    )
    assert requirements.status_code == 200
    assert requirements.json()["total"] == 1
    assert requirements.json()["items"][0]["state"] == "verified"


def test_extract_route_says_when_ai_is_not_configured(client, fake_email):
    owner = verify(client, fake_email, email="noai@example.com")
    headers = auth_header(owner["access_token"])
    _, document_id = _create_tender_and_document(client, headers)
    response = client.post(
        f"/api/tender-documents/{document_id}/extract", headers=headers
    )
    assert response.status_code == 503
