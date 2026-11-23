from __future__ import annotations

import uuid

from app.core.db import SessionLocal
from app.core.time import utcnow
from app.models.corpus import Requirement
from tests.conftest import SAMPLE_PAGES, make_pdf
from tests.helpers import auth_header, verify

TURNOVER_EXCERPT = (
    "The bidder shall have an average annual turnover of at least INR 5 crore "
    "during the last three financial years."
)
BID_EXCERPT = (
    "Bids must be submitted before 15 November 2026 at 17:00 IST through the "
    "online portal."
)
PENALTY_EXCERPT = (
    "Liquidated damages of 0.5 percent of the contract value per week of delay "
    "shall apply, subject to a maximum of 10 percent."
)


def _create_tender(
    client,
    headers,
    *,
    title="Construction of road",
    pages=None,
    source_url="https://example.test/tender/1",
):
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
        data={
            "work_title": title,
            "authority": "NHAI",
            "reference": "NHAI/2026/1",
            "location": "Maharashtra",
            "region": "Maharashtra",
            "closing_at": "2026-12-31T12:00:00+00:00",
            "source_url": source_url,
        },
    )
    assert response.status_code == 201, response.text
    tender = response.json()
    documents = client.get(
        f"/api/tenders/{tender['id']}/documents", headers=headers
    )
    assert documents.status_code == 200, documents.text
    document = documents.json()["items"][0]
    return tender, document["id"]


def _create_requirement(
    client,
    headers,
    *,
    tender_id,
    document_id,
    excerpt=TURNOVER_EXCERPT,
    page_index=1,
    **overrides,
):
    payload = {
        "document_id": document_id,
        "page_index": page_index,
        "excerpt": excerpt,
        "category": "eligibility",
        "phase": "pre_award",
        "title": "Average annual turnover",
        "obligation": (
            "Submit audited financial statements showing average annual turnover "
            "of at least INR 5 crore."
        ),
        "mandatory_state": "mandatory",
        "clause_ref": "Clause 3",
        "acceptance_criteria": "Average annual turnover of at least INR 5 crore.",
        "evidence_check": "Audited financial statements for the last three years.",
        "issuer": "NHAI",
    }
    payload.update(overrides)
    response = client.post(
        f"/api/tenders/{tender_id}/requirements", headers=headers, json=payload
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_company_document(
    client,
    headers,
    *,
    title="Audited financial statements FY 2025",
    text="Audited financial statements for three years. Average annual turnover INR 6 crore.",
    expires_on="2027-03-31",
):
    response = client.post(
        "/api/company/documents",
        headers=headers,
        files={
            "file": (
                "evidence.pdf",
                make_pdf([text]),
                "application/pdf",
            )
        },
        data={
            "document_type": "turnover",
            "title": title,
            "issuer": "Chartered Accountant",
            "signatory": "CA R. Mehta",
            "expires_on": expires_on,
            "notes": "Average annual turnover and audited financial statements",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _confirm(
    client,
    headers,
    *,
    tender_id,
    requirement_id,
    document_id,
    status="covered",
):
    return client.post(
        f"/api/tenders/{tender_id}/requirements/{requirement_id}/evidence",
        headers=headers,
        json={"document_id": document_id, "status": status},
    )


def _assessment(client, headers, tender_id):
    response = client.get(
        f"/api/tenders/{tender_id}/assessment", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_gate_unknown_until_evidence_is_human_confirmed(client, fake_email):
    owner = verify(client, fake_email, email="assess@example.com")
    headers = auth_header(owner["access_token"])
    response = client.patch(
        "/api/company",
        headers=headers,
        json={"sectors": ["road construction"]},
    )
    assert response.status_code == 200, response.text

    tender, document_id = _create_tender(client, headers)
    requirement = _create_requirement(
        client, headers, tender_id=tender["id"], document_id=document_id
    )
    assert requirement["state"] == "verified"

    company_document = _upload_company_document(client, headers)

    before = _assessment(client, headers, tender["id"])
    assert before["eligibility"]["status"] == "unknown"
    assert before["eligibility"]["pass_count"] == 0
    assert before["counts"]["mandatory_gating"] == 1
    assert before["score"]["dimensions"][0]["contribution"] == 25.0
    assert before["score"]["value"] == 45.0

    evidence = client.get(
        f"/api/tenders/{tender['id']}/requirements/{requirement['id']}/evidence",
        headers=headers,
    )
    assert evidence.status_code == 200, evidence.text
    evidence_body = evidence.json()
    assert evidence_body["status"] == "suggested"
    assert evidence_body["candidates"]
    assert evidence_body["candidates"][0]["document_id"] == company_document["id"]

    confirmed = _confirm(
        client,
        headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=company_document["id"],
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "covered"
    assert confirmed.json()["document_version_id"]

    after = _assessment(client, headers, tender["id"])
    assert after["eligibility"]["status"] == "pass"
    assert after["eligibility"]["pass_count"] == 1
    assert after["eligibility"]["items"][0]["evidence_status"] == "covered"
    assert after["score"]["value"] == 85.0
    dimension = next(
        item
        for item in after["score"]["dimensions"]
        if item["key"] == "evidence_coverage"
    )
    assert dimension["contribution"] == 40.0


def test_expiry_clears_confirmation_and_contradiction_fails(client, fake_email):
    owner = verify(client, fake_email, email="expiry@example.com")
    headers = auth_header(owner["access_token"])
    tender, document_id = _create_tender(client, headers)
    requirement = _create_requirement(
        client, headers, tender_id=tender["id"], document_id=document_id
    )

    expired = _upload_company_document(
        client,
        headers,
        title="Old audited statements",
        text="Old audited statements. Average annual turnover INR 6 crore.",
        expires_on="2020-01-01",
    )
    refused = _confirm(
        client,
        headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=expired["id"],
    )
    assert refused.status_code == 422, refused.text
    assert "expired" in refused.text.lower()

    current = _upload_company_document(
        client,
        headers,
        title="Current audited statements",
        text="Current audited statements. Average annual turnover INR 6 crore.",
        expires_on="2030-01-01",
    )
    confirmed = _confirm(
        client,
        headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=current["id"],
    )
    assert confirmed.status_code == 200, confirmed.text
    assert _assessment(client, headers, tender["id"])["eligibility"]["status"] == "pass"

    patched = client.patch(
        f"/api/company/documents/{current['id']}",
        headers=headers,
        json={"expires_on": "2020-01-01"},
    )
    assert patched.status_code == 200, patched.text

    after_expiry = _assessment(client, headers, tender["id"])
    assert after_expiry["eligibility"]["status"] == "unknown"
    assert after_expiry["eligibility"]["items"][0]["evidence_status"] == "expired"
    evidence = client.get(
        f"/api/tenders/{tender['id']}/requirements/{requirement['id']}/evidence",
        headers=headers,
    ).json()
    assert evidence["status"] == "expired"
    assert evidence["confirmed_at"] is None

    client.patch(
        f"/api/company/documents/{current['id']}",
        headers=headers,
        json={"expires_on": "2030-01-01"},
    )
    contradicted = _confirm(
        client,
        headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=current["id"],
        status="contradicted",
    )
    assert contradicted.status_code == 200, contradicted.text
    failed = _assessment(client, headers, tender["id"])
    assert failed["eligibility"]["status"] == "fail"
    assert failed["eligibility"]["items"][0]["evidence_status"] == "contradicted"


def test_accepted_ocr_is_human_confirmed_and_can_pass(client, fake_email):
    owner = verify(client, fake_email, email="ocr@example.com")
    headers = auth_header(owner["access_token"])
    tender, document_id = _create_tender(client, headers)
    requirement = _create_requirement(
        client, headers, tender_id=tender["id"], document_id=document_id
    )
    company_document = _upload_company_document(client, headers)

    reviewed = client.patch(
        f"/api/requirements/{requirement['id']}",
        headers=headers,
        json={"state": "needs_review", "note": "Check the scan."},
    )
    assert reviewed.status_code == 200, reviewed.text
    confirmed = _confirm(
        client,
        headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=company_document["id"],
    )
    assert confirmed.json()["status"] == "covered"
    needs_review = _assessment(client, headers, tender["id"])
    assert needs_review["eligibility"]["status"] == "unknown"
    assert needs_review["eligibility"]["items"][0]["citation_state"] == "needs_review"

    with SessionLocal() as session:
        row = session.get(Requirement, uuid.UUID(requirement["id"]))
        row.state = "accepted_ocr"
        row.reviewed_by = uuid.UUID(owner["user"]["id"])
        row.reviewed_at = utcnow()
        session.commit()

    accepted = _assessment(client, headers, tender["id"])
    item = accepted["eligibility"]["items"][0]
    assert accepted["eligibility"]["status"] == "pass"
    assert item["citation_state"] == "accepted_ocr"
    assert item["ocr_confirmed"] is True
    assert "OCR" in item["reason"]


def test_checklist_is_tender_specific_idempotent_and_copies_citation(
    client, fake_email
):
    owner = verify(client, fake_email, email="checklist@example.com")
    headers = auth_header(owner["access_token"])
    tender, document_id = _create_tender(client, headers)
    requirement = _create_requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt=BID_EXCERPT,
        page_index=2,
        category="submission",
        title="Submit bid online",
        obligation="Submit the bid through the online portal before the deadline.",
        clause_ref="Clause 5",
        form_ref="Form 5",
        acceptance_criteria="Bid submitted before 15 November 2026 at 17:00 IST.",
        consequence_text="Late bids shall be rejected.",
        evidence_check=None,
    )
    assert requirement["state"] == "verified"

    generated = client.post(
        f"/api/tenders/{tender['id']}/checklist/generate", headers=headers
    )
    assert generated.status_code == 200, generated.text
    first = generated.json()
    assert first["created"] == 1, first
    task = first["items"][0]
    assert task["phase"] == "pre_award"
    assert task["document_id"] == document_id
    assert task["page_index"] == 2
    assert task["clause_ref"] == "Clause 5"
    assert task["excerpt"] == BID_EXCERPT
    assert task["source_url"] == "https://example.test/tender/1"
    assert task["consequence"] == "Late bids shall be rejected."
    assert task["status"] == "open"

    updated = client.patch(
        f"/api/checklist/{task['id']}",
        headers=headers,
        json={"status": "done", "owner": "Asha"},
    )
    assert updated.status_code == 200, updated.text

    regenerated = client.post(
        f"/api/tenders/{tender['id']}/checklist/generate", headers=headers
    )
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()["created"] == 0
    assert regenerated.json()["skipped"] == 1

    listed = client.get(
        f"/api/tenders/{tender['id']}/checklist", headers=headers
    )
    assert listed.status_code == 200, listed.text
    kept = listed.json()["items"][0]
    assert kept["status"] == "done"
    assert kept["owner"] == "Asha"
    assert kept["excerpt"] == BID_EXCERPT

    post_award = _create_requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt=PENALTY_EXCERPT,
        page_index=3,
        category="performance",
        phase="post_award",
        title="Liquidated damages",
        obligation="Pay liquidated damages for delay.",
        mandatory_state="mandatory",
        clause_ref="Clause 8",
        form_ref=None,
        consequence_text="Maximum 10 percent of contract value.",
        acceptance_criteria=None,
    )
    assert post_award["state"] == "verified"
    generated_again = client.post(
        f"/api/tenders/{tender['id']}/checklist/generate", headers=headers
    ).json()
    assert generated_again["created"] == 1
    assert generated_again["skipped"] == 1
    obligation = generated_again["items"][0]
    assert obligation["phase"] == "post_award"
    assert obligation["consequence"] == "Maximum 10 percent of contract value."
    assert obligation["due_at"] is None

    manual = client.post(
        "/api/checklist",
        headers=headers,
        json={
            "tender_id": tender["id"],
            "title": "Submit original EMD",
            "category": "submission",
            "phase": "pre_award",
            "due_at": "2026-12-30T12:00:00+00:00",
        },
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["status"] == "open"


def test_legal_obligations_are_cited_and_kept_separate(client, fake_email):
    owner = verify(client, fake_email, email="legal@example.com")
    headers = auth_header(owner["access_token"])
    tender, document_id = _create_tender(client, headers)
    _create_requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt=BID_EXCERPT,
        page_index=2,
        category="compliance",
        title="Late bids rejected",
        obligation="Submit before the stated deadline.",
        mandatory_state="mandatory",
        clause_ref="Clause 5",
        consequence_text="Late bids shall be rejected.",
        acceptance_criteria=None,
    )
    _create_requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt=PENALTY_EXCERPT,
        page_index=3,
        category="performance",
        phase="post_award",
        title="Liquidated damages",
        obligation="Pay liquidated damages for delay.",
        mandatory_state="mandatory",
        clause_ref="Clause 8",
        consequence_text="Maximum 10 percent of contract value.",
        acceptance_criteria=None,
    )

    response = client.get(
        f"/api/tenders/{tender['id']}/legal", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    pre = next(item for item in body["items"] if item["phase"] == "pre_award")
    post = next(item for item in body["items"] if item["phase"] == "post_award")
    assert pre["consequence_text"] == "Late bids shall be rejected."
    assert post["consequence_text"] == "Maximum 10 percent of contract value."
    assert pre["source_url"] == "https://example.test/tender/1"
    assert pre["retrieved_at"] is not None
    assert post["clause_ref"] == "Clause 8"
    assert "not legal advice" in body["disclaimer"]


def test_assessment_and_evidence_are_company_scoped(client, fake_email):
    first = verify(client, fake_email, email="first@example.com")
    first_headers = auth_header(first["access_token"])
    tender, document_id = _create_tender(client, first_headers)
    requirement = _create_requirement(
        client, first_headers, tender_id=tender["id"], document_id=document_id
    )
    first_document = _upload_company_document(client, first_headers)

    second = verify(
        client,
        fake_email,
        email="second@example.com",
        company_name="Other Infra",
    )
    second_headers = auth_header(second["access_token"])

    visible = client.get(f"/api/tenders/{tender['id']}", headers=second_headers)
    assert visible.status_code == 200, visible.text
    assert visible.json()["requirement_count"] == 1

    second_assessment = _assessment(client, second_headers, tender["id"])
    assert second_assessment["eligibility"]["status"] == "unknown"
    assert second_assessment["eligibility"]["items"][0]["evidence_status"] == "none"

    second_evidence = client.get(
        f"/api/tenders/{tender['id']}/requirements/{requirement['id']}/evidence",
        headers=second_headers,
    )
    assert second_evidence.status_code == 200, second_evidence.text
    assert second_evidence.json()["candidates"] == []

    denied = _confirm(
        client,
        second_headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=first_document["id"],
    )
    assert denied.status_code == 404, denied.text

    confirmed = _confirm(
        client,
        first_headers,
        tender_id=tender["id"],
        requirement_id=requirement["id"],
        document_id=first_document["id"],
    )
    assert confirmed.status_code == 200, confirmed.text
    assert _assessment(client, first_headers, tender["id"])["eligibility"]["status"] == "pass"
    assert _assessment(client, second_headers, tender["id"])["eligibility"]["status"] == "unknown"
