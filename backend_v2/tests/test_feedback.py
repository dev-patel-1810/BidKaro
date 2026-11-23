"""Human feedback: "not relevant" hides one tender and demotes its work.

The rule under test is narrow on purpose. A human rejection is a *small*
negative weight on similar work and a dismissal of that one tender. It is never
a ban: the company's declared word keeps matching other tenders, and a tender
that only matched because of the rejection is still visible.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.intelligence import CompanyRelevanceFeedback
from app.repositories.corpus import TenderRepository
from tests.helpers import auth_header, verify

#: Relative to the real clock: a fixed date would eventually look closed to the
#: default list, which now hides closed tenders.
NOW = datetime.now(timezone.utc)


def _store(title, *, days=30):
    with SessionLocal() as session:
        tender = TenderRepository(session).create(
            {
                "dedupe_key": f"test:{uuid.uuid4()}",
                "portal_tender_id": f"2027_T_{uuid.uuid4().hex[:8]}_1",
                "work_title": title,
                "location": "Maharashtra",
                "region": "Maharashtra",
                "closing_at": NOW + timedelta(days=days),
                "source_type": "official_portal",
                "extracted_from": "notice_page",
                "created_at": NOW,
            }
        )
        session.commit()
        return str(tender.id)


def _company(client, fake_email, *, email="feedback@example.com"):
    owner = verify(client, fake_email, email=email)
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text
    return owner, headers


def _ids(response):
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def _background(count: int = 20) -> None:
    """Fill the corpus with work the company does not declare.

    Rarity is read off the corpus, so a two-tender corpus would say "road" is
    everywhere. A realistic mix is what makes "road" specific.
    """

    for index in range(count):
        _store(f"Housekeeping and cleaning of block {index}")


def _feedback_rows(tender_id: str):
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(CompanyRelevanceFeedback).where(
                    CompanyRelevanceFeedback.tender_id == uuid.UUID(tender_id)
                )
            ).all()
        )


def test_not_relevant_hides_the_tender_and_records_the_signal(client, fake_email):
    owner, headers = _company(client, fake_email)
    first = _store("Road resurfacing of NH-48")
    second = _store("Road repair of SH-1")

    rejected = client.post(f"/api/tenders/{first}/not-relevant", headers=headers)
    assert rejected.status_code == 200, rejected.text

    listed = client.get("/api/tenders", headers=headers)
    assert _ids(listed) == [second]

    # The dismissal is the same one "dismiss" already honours; the record is
    # what makes the next similar tender rank lower.
    with SessionLocal() as session:
        feedback = session.scalars(
            select(CompanyRelevanceFeedback).where(
                CompanyRelevanceFeedback.tender_id == uuid.UUID(first)
            )
        ).all()
    assert len(feedback) == 1
    assert feedback[0].verdict == "not_relevant"
    assert feedback[0].matched_terms == ["road"]

    # Still reachable when dismissed rows are asked for: hidden is not deleted.
    everything = client.get(
        "/api/tenders", headers=headers, params={"include_dismissed": True}
    )
    assert set(_ids(everything)) == {first, second}


def test_a_rejection_demotes_similar_work_without_hiding_it(client, fake_email):
    owner, headers = _company(client, fake_email)
    _background()
    first = _store("Road resurfacing of NH-48")
    second = _store("Road repair of SH-1")

    before = client.get("/api/tenders", headers=headers).json()["items"]
    before_scores = {item["id"]: item["relevance"]["match_score"] for item in before}
    assert before_scores[first] == before_scores[second]

    client.post(f"/api/tenders/{first}/not-relevant", headers=headers)

    after = client.get("/api/tenders", headers=headers).json()["items"]
    assert [item["id"] for item in after] == [second]
    # The sibling is still listed, at a lower score, and still says why.
    assert after[0]["relevance"]["match_score"] < before_scores[second]
    assert "road" in after[0]["relevance"]["matched_terms"]


def test_saving_a_tender_records_positive_feedback(client, fake_email):
    owner, headers = _company(client, fake_email)
    tender_id = _store("Road resurfacing of NH-48")

    saved = client.post(f"/api/tenders/{tender_id}/save", headers=headers)
    assert saved.status_code == 200, saved.text

    with SessionLocal() as session:
        feedback = session.scalars(
            select(CompanyRelevanceFeedback).where(
                CompanyRelevanceFeedback.tender_id == uuid.UUID(tender_id)
            )
        ).all()
    assert len(feedback) == 1
    assert feedback[0].verdict == "relevant"


def test_feedback_is_scoped_to_the_company_that_gave_it(client, fake_email):
    owner, headers = _company(client, fake_email, email="one@example.com")
    tender_id = _store("Road resurfacing of NH-48")
    client.post(f"/api/tenders/{tender_id}/not-relevant", headers=headers)

    other_owner = verify(client, fake_email, email="two@example.com")
    other_headers = auth_header(other_owner["access_token"])
    client.patch(
        "/api/company",
        headers=other_headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )

    # One company's dismissal must not hide the corpus for another than itself.
    assert _ids(client.get("/api/tenders", headers=other_headers)) == [tender_id]


def test_a_rejected_word_still_matches_other_tenders(client, fake_email):
    owner, headers = _company(client, fake_email)
    first = _store("Road resurfacing of NH-48")
    second = _store("Road repair of SH-1")

    client.post(f"/api/tenders/{first}/not-relevant", headers=headers)
    third = _store("Road rebuilding of the bypass")

    # "road" was rejected once, and it is still the word that lists the corpus
    # the company asked for. Demotion, never a ban.
    assert set(_ids(client.get("/api/tenders", headers=headers))) == {second, third}


def test_removing_a_save_withdraws_the_positive_signal(client, fake_email):
    owner, headers = _company(client, fake_email, email="unsave@example.com")
    tender_id = _store("Road resurfacing of NH-48")

    client.post(f"/api/tenders/{tender_id}/save", headers=headers)
    assert [row.verdict for row in _feedback_rows(tender_id)] == ["relevant"]

    removed = client.delete(f"/api/tenders/{tender_id}/save", headers=headers)
    assert removed.status_code == 200, removed.text

    # An unsaved tender is no longer a preference the matcher should keep
    # using, so the signal goes with the bookmark.
    assert _feedback_rows(tender_id) == []


def test_not_relevant_removes_a_saved_tender_from_my_tenders(client, fake_email):
    owner, headers = _company(client, fake_email, email="savedreject@example.com")
    tender_id = _store("Road resurfacing of NH-48")

    client.post(f"/api/tenders/{tender_id}/save", headers=headers)
    assert _ids(client.get("/api/tenders?mine=true", headers=headers)) == [tender_id]

    rejected = client.post(f"/api/tenders/{tender_id}/not-relevant", headers=headers)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["saved"] is False

    # The saved list drops it (a dismissal outranks a bookmark) and the
    # negative signal replaces the positive one. The bookmark itself is
    # withdrawn too, so clearing the dismissal later cannot resurrect it.
    assert client.get("/api/tenders?mine=true", headers=headers).json()["items"] == []
    assert [row.verdict for row in _feedback_rows(tender_id)] == ["not_relevant"]
