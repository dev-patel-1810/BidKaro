from __future__ import annotations

import re
import uuid

from app.api.deps import get_ai_provider
from app.core.db import SessionLocal
from app.main import app
from app.services.company_intel import CompanyDocumentIntelService
from tests.conftest import make_pdf
from tests.helpers import auth_header, verify

PROFILE_PAGES = [
    (
        "DCS Infra provides document scanning and document digitisation for "
        "court records. Our team completed the digitisation of land records "
        "for the Jammu and Kashmir High Court. We also provide data entry and "
        "metadata indexing."
    ),
    (
        "Selected projects: Digitisation of revenue records for the Revenue "
        "Department, completed in 2024. Products: DCS ScanSuite document "
        "management software."
    ),
]

CAPABILITY_QUOTE = (
    "DCS Infra provides document scanning and document digitisation for "
    "court records."
)
EXPERIENCE_QUOTE = (
    "Digitisation of revenue records for the Revenue Department, completed in "
    "2024."
)


class UnderstandingAI:
    model = "fake-understanding"
    configured = True

    def complete_json(self, *, system: str, user: str) -> dict:
        return {
            "capabilities": [
                {
                    "name": "Document digitisation",
                    "description": "Scanning paper records into searchable digital files.",
                    "aliases": ["document scanning", "digitisation of records"],
                    "page": 1,
                    "quote": CAPABILITY_QUOTE,
                },
                {
                    # The words are not on the page: must be dropped.
                    "name": "Aerial survey",
                    "description": "Drone surveys.",
                    "aliases": [],
                    "page": 1,
                    "quote": "We provide aerial survey of highways and bridges.",
                },
                {
                    # The company's own name is not a kind of work.
                    "name": "Acme Infra",
                    "description": "The company.",
                    "aliases": [],
                    "page": 1,
                    "quote": CAPABILITY_QUOTE,
                },
            ],
            "experiences": [
                {
                    "title": "Revenue records digitisation",
                    "client": "Revenue Department",
                    "description": "Digitised land revenue records.",
                    "work_family": "Document digitisation",
                    "page": 2,
                    "quote": EXPERIENCE_QUOTE,
                },
                {
                    "title": "Aerial survey of bridges",
                    "client": None,
                    "description": "Not in the file.",
                    "work_family": None,
                    "page": 2,
                    "quote": "We surveyed forty bridges by drone.",
                },
            ],
        }


class SimilarityAI:
    model = "fake-similarity"
    configured = True

    def __init__(self, *, answer: str, anchor: str, capability: str) -> None:
        self.answer = answer
        self.anchor = anchor
        self.capability = capability

    def complete_json(self, *, system: str, user: str) -> dict:
        ids = re.findall(r"tender_id: ([0-9a-f-]{36})", user)
        return {
            "judgments": [
                {
                    "tender_id": tender_id,
                    "answer": self.answer,
                    "anchor": self.anchor,
                    "capability": self.capability,
                }
                for tender_id in ids
            ]
        }


class EvidenceAI:
    """A fake that answers with the new two-sided evidence fields."""

    model = "fake-evidence"
    configured = True

    def __init__(
        self,
        *,
        answer: str,
        tender_evidence: str,
        company_evidence: str,
        match_type: str = "exact_core",
        reason: str = "The notice and the brief describe the same work.",
    ) -> None:
        self.answer = answer
        self.tender_evidence = tender_evidence
        self.company_evidence = company_evidence
        self.match_type = match_type
        self.reason = reason

    def complete_json(self, *, system: str, user: str) -> dict:
        ids = re.findall(r"tender_id: ([0-9a-f-]{36})", user)
        return {
            "judgments": [
                {
                    "tender_id": tender_id,
                    "title": "Digitisation of court records",
                    "answer": self.answer,
                    "tender_evidence": self.tender_evidence,
                    "company_evidence": self.company_evidence,
                    "match_type": self.match_type,
                    "reason": self.reason,
                }
                for tender_id in ids
            ]
        }


def _upload_profile(client, headers) -> dict:
    response = client.post(
        "/api/company/documents",
        headers=headers,
        files={
            "file": (
                "profile.pdf",
                make_pdf(PROFILE_PAGES),
                "application/pdf",
            )
        },
        data={"document_type": "about_company", "title": "Company profile"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _understand(client, headers, document_id: str) -> dict:
    with SessionLocal() as session:
        result = CompanyDocumentIntelService(session, ai=UnderstandingAI()).extract(
            document_id=uuid.UUID(document_id)
        )
    assert result["state"] == "done", result
    response = client.get("/api/company/understanding", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _confirmed_company(client, fake_email, *, email: str = "understood@example.com"):
    owner = verify(client, fake_email, email=email)
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["document digitisation"], "region": "Jammu"},
    )
    assert profile.status_code == 200, profile.text
    document = _upload_profile(client, headers)
    understanding = _understand(client, headers, document["id"])
    for item in understanding["capabilities"]:
        confirmed = client.patch(
            f"/api/company/capabilities/{item['id']}",
            headers=headers,
            json={"state": "confirmed"},
        )
        assert confirmed.status_code == 200, confirmed.text
    for item in understanding["experiences"]:
        confirmed = client.patch(
            f"/api/company/experiences/{item['id']}",
            headers=headers,
            json={"state": "confirmed"},
        )
        assert confirmed.status_code == 200, confirmed.text
    return headers, document, understanding


def _tender(client, headers, *, title="Digitisation of court records"):
    response = client.post(
        "/api/tenders",
        headers=headers,
        files={
            "file": (
                "tender.pdf",
                make_pdf(
                    [
                        "The bidder shall digitise court records for the district courts.",
                        "The bidder shall provide aerial survey of highways.",
                    ]
                ),
                "application/pdf",
            )
        },
        data={
            "work_title": title,
            "authority": "District Court",
            "reference": "DC/2026/17",
            "location": "Jammu",
            "region": "Jammu",
            "closing_at": "2027-01-31T12:00:00+00:00",
            "source_url": "https://example.test/tender/9",
        },
    )
    assert response.status_code == 201, response.text
    tender = response.json()
    documents = client.get(
        f"/api/tenders/{tender['id']}/documents", headers=headers
    )
    assert documents.status_code == 200, documents.text
    return tender, documents.json()["items"][0]["id"]


def _requirement(client, headers, *, tender_id, document_id, excerpt, page_index=1):
    response = client.post(
        f"/api/tenders/{tender_id}/requirements",
        headers=headers,
        json={
            "document_id": document_id,
            "page_index": page_index,
            "excerpt": excerpt,
            "category": "performance",
            "phase": "pre_award",
            "title": excerpt[:60],
            "mandatory_state": "mandatory",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_understanding_keeps_only_quoted_items_with_their_page(client, fake_email):
    owner = verify(client, fake_email, email="quote@example.com")
    headers = auth_header(owner["access_token"])
    document = _upload_profile(client, headers)

    understanding = _understand(client, headers, document["id"])

    names = [item["name"] for item in understanding["capabilities"]]
    assert names == ["Document digitisation"]
    capability = understanding["capabilities"][0]
    assert capability["excerpt"] == CAPABILITY_QUOTE
    assert capability["page_index"] == 1
    assert capability["quote_verified"] is True
    assert capability["state"] == "proposed"
    assert capability["aliases"] == ["document scanning", "digitisation of records"]

    # The experience whose quote was not in the file is dropped, not shown.
    titles = [item["title"] for item in understanding["experiences"]]
    assert titles == ["Revenue records digitisation"]
    experience = understanding["experiences"][0]
    assert experience["excerpt"] == EXPERIENCE_QUOTE
    assert experience["page_index"] == 2
    assert experience["client"] == "Revenue Department"

    assert understanding["counts"] == {
        "capabilities": 1,
        "experiences": 1,
        "confirmed_capabilities": 0,
        "confirmed_experiences": 0,
    }


def test_a_human_confirms_or_edits_a_capability(client, fake_email):
    owner = verify(client, fake_email, email="confirm@example.com")
    headers = auth_header(owner["access_token"])
    document = _upload_profile(client, headers)
    understanding = _understand(client, headers, document["id"])
    capability = understanding["capabilities"][0]

    edited = client.patch(
        f"/api/company/capabilities/{capability['id']}",
        headers=headers,
        json={
            "state": "confirmed",
            "name": "Records digitisation",
            "description": "Paper records to searchable files.",
            "aliases": ["record scanning"],
        },
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["state"] == "confirmed"
    assert body["name"] == "Records digitisation"
    assert body["confirmed_at"] is not None
    # The citation is untouched by a name edit.
    assert body["excerpt"] == CAPABILITY_QUOTE
    assert body["page_index"] == 1

    rejected = client.patch(
        f"/api/company/capabilities/{capability['id']}",
        headers=headers,
        json={"state": "rejected"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected"
    assert rejected.json()["confirmed_at"] is None


def test_similarity_yes_needs_an_anchor_from_the_notice(client, fake_email):
    headers, _document, understanding = _confirmed_company(
        client, fake_email, email="judge@example.com"
    )
    tender, _tender_document = _tender(client, headers)
    capability_name = understanding["capabilities"][0]["name"]

    app.dependency_overrides[get_ai_provider] = lambda: SimilarityAI(
        answer="yes",
        anchor="Digitisation of court records",
        capability=capability_name,
    )
    response = client.post(
        f"/api/tenders/{tender['id']}/similarity", headers=headers
    )
    assert response.status_code == 200, response.text
    similarity = response.json()["similarity"]
    assert similarity["answer"] == "yes"
    assert similarity["anchor_verified"] is True
    assert similarity["anchor_text"] == "Digitisation of court records"
    assert similarity["capabilities"] == [capability_name]
    assert similarity["needs_confirmation"] is False

    listed = client.get("/api/tenders?sort=match_desc", headers=headers)
    assert listed.status_code == 200, listed.text
    item = next(row for row in listed.json()["items"] if row["id"] == tender["id"])
    assert item["similarity"]["answer"] == "yes"


def test_an_unanchored_yes_is_a_suggestion_not_a_match(client, fake_email):
    headers, _document, understanding = _confirmed_company(
        client, fake_email, email="unanchored@example.com"
    )
    tender, _tender_document = _tender(client, headers)

    app.dependency_overrides[get_ai_provider] = lambda: SimilarityAI(
        answer="yes",
        anchor="aerial survey of bridges",
        capability=understanding["capabilities"][0]["name"],
    )
    response = client.post(
        f"/api/tenders/{tender['id']}/similarity", headers=headers
    )
    assert response.status_code == 200, response.text
    similarity = response.json()["similarity"]
    assert similarity["answer"] == "unsure"
    assert similarity["needs_confirmation"] is True
    assert similarity["anchor_text"] is None
    assert similarity["anchor_verified"] is False


def test_a_no_demotes_but_never_hides_the_tender(client, fake_email):
    headers, _document, understanding = _confirmed_company(
        client, fake_email, email="demote@example.com"
    )
    tender, _tender_document = _tender(client, headers)

    before = client.get("/api/tenders?sort=match_desc", headers=headers)
    assert before.status_code == 200, before.text
    before_row = next(
        row for row in before.json()["items"] if row["id"] == tender["id"]
    )
    score_before = before_row["relevance"]["match_score"]
    tier_before = before_row["relevance"]["tier"]
    assert tier_before == 1

    app.dependency_overrides[get_ai_provider] = lambda: SimilarityAI(
        answer="no",
        anchor="",
        capability="",
    )
    judged = client.post(
        f"/api/tenders/{tender['id']}/similarity", headers=headers
    )
    assert judged.status_code == 200, judged.text
    assert judged.json()["similarity"]["answer"] == "no"

    after = client.get("/api/tenders?sort=match_desc", headers=headers)
    assert after.status_code == 200, after.text
    row = next(
        item for item in after.json()["items"] if item["id"] == tender["id"]
    )
    assert row["similarity"]["answer"] == "no"
    assert row["relevance"]["tier"] == 6
    assert row["relevance"]["bucket"] == "possible"
    assert row["relevance"]["match_score"] < score_before
    assert 0.40 <= row["relevance"]["match_score"] <= 0.48


class MappingAI:
    model = "fake-mapping"
    configured = True

    def __init__(self, *, covered_id: str, new_id: str, experience_id: str) -> None:
        self.covered_id = covered_id
        self.new_id = new_id
        self.experience_id = experience_id

    def complete_json(self, *, system: str, user: str) -> dict:
        return {
            "matches": [
                {
                    "requirement_id": self.covered_id,
                    "coverage": "covered",
                    "experience_ids": [self.experience_id],
                    "capability_ids": [],
                },
                {
                    "requirement_id": self.new_id,
                    "coverage": "new",
                    "experience_ids": [],
                    "capability_ids": [],
                },
            ]
        }


def test_requirements_are_mapped_to_experience_with_both_citations(
    client, fake_email
):
    headers, document, understanding = _confirmed_company(
        client, fake_email, email="mapping@example.com"
    )
    tender, document_id = _tender(client, headers)
    digitise = _requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt="The bidder shall digitise court records for the district courts.",
        page_index=1,
    )
    survey = _requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt="The bidder shall provide aerial survey of highways.",
        page_index=2,
    )
    experience_id = understanding["experiences"][0]["id"]

    app.dependency_overrides[get_ai_provider] = lambda: MappingAI(
        covered_id=digitise["id"],
        new_id=survey["id"],
        experience_id=experience_id,
    )
    response = client.post(
        f"/api/tenders/{tender['id']}/requirement-experience", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["covered"] == 1
    assert body["counts"]["new"] == 1

    by_id = {item["requirement_id"]: item for item in body["items"]}
    covered = by_id[digitise["id"]]
    assert covered["coverage"] == "covered"
    assert covered["experiences"] == ["Revenue records digitisation"]
    assert covered["company_citation"]["document_id"] == document["id"]
    assert covered["company_citation"]["page_index"] == 2
    assert covered["company_citation"]["excerpt"] == EXPERIENCE_QUOTE
    assert covered["requirement_excerpt"].startswith("The bidder shall digitise")

    new_work = by_id[survey["id"]]
    assert new_work["coverage"] == "new"
    assert new_work["company_citation"] is None
    assert "not a failure" in new_work["reason"]

    # Repeating the comparison does not ask the same question twice.
    second = client.post(
        f"/api/tenders/{tender['id']}/requirement-experience", headers=headers
    )
    assert second.status_code == 200, second.text
    assert second.json()["counts"] == body["counts"]


def test_checklist_says_when_a_requirement_is_new_work(client, fake_email):
    headers, _document, understanding = _confirmed_company(
        client, fake_email, email="checklist@example.com"
    )
    tender, document_id = _tender(client, headers)
    digitise = _requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt="The bidder shall digitise court records for the district courts.",
        page_index=1,
    )
    survey = _requirement(
        client,
        headers,
        tender_id=tender["id"],
        document_id=document_id,
        excerpt="The bidder shall provide aerial survey of highways.",
        page_index=2,
    )
    app.dependency_overrides[get_ai_provider] = lambda: MappingAI(
        covered_id=digitise["id"],
        new_id=survey["id"],
        experience_id=understanding["experiences"][0]["id"],
    )
    mapped = client.post(
        f"/api/tenders/{tender['id']}/requirement-experience", headers=headers
    )
    assert mapped.status_code == 200, mapped.text

    generated = client.post(
        f"/api/tenders/{tender['id']}/checklist/generate", headers=headers
    )
    assert generated.status_code == 200, generated.text
    tasks = client.get(
        f"/api/checklist?tender_id={tender['id']}", headers=headers
    )
    assert tasks.status_code == 200, tasks.text
    by_requirement = {
        item["requirement_id"]: item for item in tasks.json()["items"]
    }
    assert by_requirement[survey["id"]]["evidence_check"].startswith("New work:")
    assert "Your profile shows related work" in by_requirement[digitise["id"]][
        "evidence_check"
    ]

def test_batch_similarity_needs_a_confirmed_capability_first(client, fake_email):
    owner = verify(client, fake_email, email="batch-empty@example.com")
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["document digitisation"], "region": "Jammu"},
    )
    assert profile.status_code == 200, profile.text
    tender, _document_id = _tender(client, headers)

    response = client.post(
        "/api/discovery/similarity",
        headers=headers,
        json={"tender_ids": [tender["id"]]},
    )
    assert response.status_code == 422, response.text
    assert "capability" in response.json()["detail"]


def test_batch_similarity_enqueues_one_job_for_the_visible_tenders(
    client, fake_email
):
    headers, _document, _understanding = _confirmed_company(
        client, fake_email, email="batch-job@example.com"
    )
    tender, _document_id = _tender(client, headers)

    started = client.post(
        "/api/discovery/similarity",
        headers=headers,
        json={"tender_ids": [tender["id"]]},
    )
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["created"] is True
    assert body["job"]["kind"] == "judge_tender_similarity"

    # Asking again while the first job is queued returns it instead of
    # stacking a second one.
    again = client.post(
        "/api/discovery/similarity",
        headers=headers,
        json={"tender_ids": [tender["id"]]},
    )
    assert again.status_code == 202, again.text
    assert again.json()["created"] is False
    assert again.json()["job"]["id"] == body["job"]["id"]


def test_evidence_aware_judgment_stores_both_exact_lines(client, fake_email):
    headers, _document, _understanding = _confirmed_company(
        client, fake_email, email="evidence@example.com"
    )
    tender, _document_id = _tender(client, headers)

    app.dependency_overrides[get_ai_provider] = lambda: EvidenceAI(
        answer="yes",
        tender_evidence="Digitisation of court records",
        company_evidence=CAPABILITY_QUOTE,
        match_type="exact_core",
    )
    response = client.post(
        f"/api/tenders/{tender['id']}/similarity", headers=headers
    )
    assert response.status_code == 200, response.text
    similarity = response.json()["similarity"]

    assert similarity["answer"] == "yes"
    assert similarity["title"] == "Digitisation of court records"
    assert similarity["tender_evidence"] == "Digitisation of court records"
    assert similarity["company_evidence"] == CAPABILITY_QUOTE
    # The model's match_type is advice; the stored type comes from the role of
    # the company line it actually quoted.
    assert similarity["match_type"] == "exact_core"
    assert similarity["evidence_rank"] == 1
    assert similarity["needs_confirmation"] is False

    listed = client.get("/api/tenders?sort=match_desc", headers=headers)
    item = next(row for row in listed.json()["items"] if row["id"] == tender["id"])
    assert item["similarity"]["company_evidence"] == CAPABILITY_QUOTE
    assert item["relevance"]["tier"] == 1
    assert item["relevance"]["bucket"] == "strong"


def test_a_paraphrased_company_quote_is_not_evidence(client, fake_email):
    headers, _document, _understanding = _confirmed_company(
        client, fake_email, email="paraphrase@example.com"
    )
    tender, _document_id = _tender(client, headers)

    app.dependency_overrides[get_ai_provider] = lambda: EvidenceAI(
        answer="yes",
        tender_evidence="Digitisation of court records",
        company_evidence="We scan court papers.",
        match_type="exact_core",
    )
    response = client.post(
        f"/api/tenders/{tender['id']}/similarity", headers=headers
    )
    assert response.status_code == 200, response.text
    similarity = response.json()["similarity"]

    assert similarity["answer"] == "unsure"
    assert similarity["needs_confirmation"] is True
    assert similarity["company_evidence"] is None
    assert similarity["match_type"] == "no_evidence"
    assert similarity["evidence_rank"] == 6


def test_a_support_only_yes_stays_in_the_possible_band(client, fake_email):
    headers, _document, _understanding = _confirmed_company(
        client, fake_email, email="support-only@example.com"
    )
    saved = client.post(
        "/api/company/brief",
        headers=headers,
        json={
            "payload": {
                "support_terms": [
                    {
                        "term": "technical support",
                        "evidence": "Our team provides technical support for the portal.",
                    }
                ],
                "adjacent_terms": [{"term": "document digitisation"}],
            }
        },
    )
    assert saved.status_code == 200, saved.text
    tender, _document_id = _tender(
        client, headers, title="Annual technical support for the portal"
    )

    app.dependency_overrides[get_ai_provider] = lambda: EvidenceAI(
        answer="yes",
        tender_evidence="Annual technical support for the portal",
        company_evidence="Our team provides technical support for the portal.",
        match_type="support_only",
    )
    response = client.post(
        f"/api/tenders/{tender['id']}/similarity", headers=headers
    )
    assert response.status_code == 200, response.text
    similarity = response.json()["similarity"]

    assert similarity["answer"] == "yes"
    assert similarity["match_type"] == "support_only"
    assert similarity["evidence_rank"] == 4

    listed = client.get("/api/tenders?sort=match_desc", headers=headers)
    item = next(row for row in listed.json()["items"] if row["id"] == tender["id"])
    assert item["relevance"]["tier"] == 5
    assert item["relevance"]["bucket"] == "possible"
