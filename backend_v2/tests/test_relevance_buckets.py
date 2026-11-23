"""The fit ladder: what is work evidence and what is not.

These tests pin the rules the Discover list depends on, so a later "helpful"
tweak cannot quietly reintroduce the false positives that made the old gate
list a scanning firm's tenders under a public works department.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.db import SessionLocal
from app.discovery.relevance import (
    GLOBAL_SCORE_CAP,
    RARE_SHARE,
    classify,
    rarity_from_titles,
    tender_matches_company,
    understanding_terms_from_rows,
)
from app.discovery.work_text import clean_term
from app.repositories.corpus import TenderRepository
from app.services.relevance import tender_profile_text
from tests.helpers import auth_header, verify

#: Relative times: the closed-tender filter compares with the real clock, so a
#: fixed past date would make every "open" tender in this file look closed.
NOW = datetime.now(timezone.utc)


def _company(
    *,
    services=(),
    sectors=(),
    keywords=(),
    understanding=(),
    region="Maharashtra",
):
    return SimpleNamespace(
        sectors=list(sectors),
        services=list(services),
        keywords=list(keywords),
        understanding=list(understanding),
        region=region,
        profile={},
    )


def _tender(title, *, authority=None, location="Maharashtra", chain=None):
    return SimpleNamespace(
        work_title=title,
        work_type=None,
        authority=authority,
        organisation_chain=chain,
        location=location,
        region=location,
        reference=None,
        source_type="official_portal",
    )


def test_the_issuing_body_is_not_work_evidence():
    """A department's name says nothing about the work it is buying."""

    company = _company(services=["wire scanning"])
    tender = _tender(
        "Housekeeping and cleaning of a district hospital",
        authority="Public Works Department (Scanning Division)",
        chain="Public Works Department||Government of Maharashtra",
    )

    verdict = classify(company, tender)

    assert verdict["bucket"] == "hidden"
    assert verdict["tier"] == 7
    assert verdict["matched_terms"] == []
    # The display helper must agree with the gate.
    assert "scanning" not in tender_profile_text(tender)


def test_the_region_alone_never_makes_a_match():
    company = _company(services=["wire scanning"])
    tender = _tender("Housekeeping of a hospital", location="Maharashtra")

    assert classify(company, tender)["bucket"] == "hidden"


def test_a_profile_phrase_is_a_strong_match():
    company = _company(services=["wire scanning"])
    tender = _tender("Wire scanning of the Ganga bridge")

    verdict = classify(company, tender)

    assert verdict["bucket"] == "strong"
    assert verdict["tier"] == 3
    assert verdict["matched_terms"] == ["wire scanning"]


def test_two_independent_terms_are_strong_evidence():
    company = _company(services=["road", "survey"])
    tender = _tender("Road survey on NH-48")

    verdict = classify(company, tender)

    assert verdict["bucket"] == "strong"
    assert verdict["tier"] == 3
    assert set(verdict["matched_terms"]) == {"road", "survey"}


def test_one_common_word_only_earns_possible():
    """A word in most of the corpus cannot be the whole evidence.

    The share is read from the candidate titles, so this is the data saying
    "road" is everywhere here, not a handwritten list calling it generic.
    """

    company = _company(services=["road"])
    titles = [f"Road repair of stretch {index}" for index in range(40)]
    rarity = rarity_from_titles(titles, ["road"])
    assert rarity["road"] >= 0.2

    verdict = classify(company, _tender("Road repair of stretch 1"), rarity=rarity)

    assert verdict["bucket"] == "possible"
    assert verdict["tier"] == 5
    assert "common" in verdict["reasons"][0]


def test_one_specific_word_is_possible_not_strong():
    """A single word is one support unit: tier 5 with the region, tier 6 without.

    This is the deliberate quality trade: only a phrase, two terms or a
    confirmed capability can make a strong match, because one word is how a
    scanning firm matched a road department.
    """

    company = _company(services=["photogrammetry"])
    rarity = {"photogrammetry": 0.0}

    verdict = classify(
        company, _tender("Photogrammetry of the canal command area"), rarity=rarity
    )

    assert verdict["bucket"] == "possible"
    assert verdict["tier"] == 5
    assert rarity["photogrammetry"] <= RARE_SHARE


def test_a_single_keyword_outside_the_region_is_possible_not_hidden():
    company = _company(keywords=["amc"], region="Maharashtra")
    outside = _tender("AMC of the water works", location="Kerala")

    verdict = classify(company, outside)

    assert verdict["bucket"] == "possible"
    assert verdict["tier"] == 6
    assert verdict["region_match"] is False


def test_a_keyword_in_the_region_is_a_better_tier():
    company = _company(keywords=["amc"], region="Maharashtra")
    nearby = _tender("AMC of the water works", location="Maharashtra")

    verdict = classify(company, nearby)

    assert verdict["bucket"] == "possible"
    assert verdict["tier"] == 5
    assert verdict["region_match"] is True


def test_negative_feedback_demotes_and_never_bans():
    company = _company(services=["road resurfacing"])
    rarity = {"road resurfacing": 0.0}
    tender = _tender("Road resurfacing of NH-48")

    before = classify(company, tender, rarity=rarity)
    after = classify(
        company,
        tender,
        rarity=rarity,
        negative={"road resurfacing": 2},
    )

    assert before["bucket"] == "strong"
    assert before["tier"] == 3
    # Demoted, but still visible: a rejected word is not a banned word. The
    # penalty is per rejected word, not per rejection, so rejecting the same
    # kind of work ten times cannot bury a tender without limit.
    assert after["bucket"] == "possible"
    assert 4 <= after["tier"] <= 6
    assert after["score"] < before["score"]
    assert after["matched_terms"] == ["road resurfacing"]
    assert any("not relevant" in reason for reason in after["reasons"])

    both = classify(
        company, tender, rarity=rarity, negative={"road": 1, "survey": 1}
    )
    assert both["score"] <= after["score"]
    # Still visible however often similar work is rejected.
    assert tender_matches_company(
        company, tender, rarity=rarity, negative={"road resurfacing": 2}
    )


def test_a_manual_upload_is_always_visible():
    company = _company(services=["wire scanning"])
    upload = _tender("Housekeeping of a hospital")
    upload.source_type = "manual_upload"

    assert tender_matches_company(company, upload)


def test_an_empty_profile_shows_the_corpus_without_claiming_a_match():
    company = _company(services=[], keywords=[])
    verdict = classify(company, _tender("Anything at all"))

    assert verdict["bucket"] == "possible"
    assert verdict["tier"] == 7
    assert verdict["profile_complete"] is False
    assert verdict["matched_terms"] == []


# -- the ladder's top rungs ------------------------------------------------


def test_a_confirmed_capability_with_a_keyword_is_the_best_match():
    company = _company(
        understanding=["Digitisation"],
        keywords=["land records"],
    )
    verdict = classify(company, _tender("Digitisation of land records"))

    assert verdict["tier"] == 1
    assert verdict["bucket"] == "strong"
    assert verdict["score"] <= GLOBAL_SCORE_CAP
    assert {"digitisation", "land records"} <= set(verdict["matched_terms"])


def test_a_confirmed_capability_alone_is_second():
    company = _company(understanding=["Digitisation"])
    verdict = classify(company, _tender("Digitisation of court records"))

    assert verdict["tier"] == 2
    assert verdict["bucket"] == "strong"
    # Even the best rung never reads 100%.
    assert verdict["score"] <= GLOBAL_SCORE_CAP


def test_places_departments_and_acronyms_are_not_work_evidence():
    company = _company(
        services=["wire scanning"],
        keywords=["jammu", "technical", "computer", "high", "dcte", "mahsar"],
        region="Jammu",
    )
    tender = _tender(
        "Supply of computers for the Department of Technical Education, Jammu",
        authority="DCTE",
        chain="DCTE||Government of Jammu and Kashmir",
        location="Jammu",
    )

    verdict = classify(company, tender)

    assert verdict["bucket"] == "hidden"
    assert verdict["matched_terms"] == []
    assert verdict["reasons"] == [
        "The work does not match your confirmed or declared work profile."
    ]


def test_generic_words_are_not_work_evidence():
    company = _company(
        services=["wire scanning"],
        keywords=["data", "entry", "software", "document", "computer"],
    )
    tender = _tender("Data entry and software supply")

    assert classify(company, tender)["bucket"] == "hidden"


def test_a_generic_word_inside_a_company_phrase_is_not_the_phrase():
    """Genericity is relative to the company's own phrase.

    "construction" is too broad to stand for "road construction", so a
    hospital building notice is not the company's work; the whole phrase is
    still evidence when the notice names the road too.
    """

    company = _company(services=["road construction"])

    other_work = classify(company, _tender("Construction of a hospital block"))
    their_work = classify(company, _tender("Road construction on NH-48"))

    assert other_work["bucket"] == "hidden"
    assert their_work["tier"] == 3


def test_a_work_word_that_is_also_an_org_word_survives_inside_its_phrase():
    """Filtering the buyer must not delete the work noun.

    "centre", "college" and "court" name a buyer in "for the ...", but they
    also name a kind of work in "data centre", "college building" and "court
    records". A cross-sector profile has to keep them inside the phrase.
    """

    data_centre = classify(
        _company(keywords=["data centre"]),
        _tender("Setting up a data centre at the campus"),
    )
    college = classify(
        _company(services=["college building"]),
        _tender("Construction of a college building"),
    )
    records = classify(
        _company(keywords=["court records"]),
        _tender("Digitisation of court records"),
    )

    assert data_centre["tier"] == 3
    assert "data centre" in data_centre["matched_terms"]
    assert college["tier"] == 3
    assert "college building" in college["matched_terms"]
    assert records["tier"] == 3
    assert "court records" in records["matched_terms"]


def test_a_place_or_buyer_phrase_is_not_a_profile_term():
    """A profile phrase is work, not where it is or who buys it."""

    assert clean_term("Jammu and Kashmir") is None
    assert clean_term("Government of India") is None
    assert clean_term("Department of Road Transport") == "road transport"
    assert clean_term("Roads in Jammu") == "roads"


def test_a_confirmed_name_cannot_be_an_org_acronym_but_an_alias_can_be_short():
    capability = SimpleNamespace(name="MAHSR", aliases=["DMS"])
    experience = SimpleNamespace(work_family="DCTE")

    terms = understanding_terms_from_rows([capability], [experience])

    assert terms == ["dms"]


def test_word_order_and_spelling_are_the_same_work():
    company = _company(services=["data processing"])
    tender = _tender("Processing of data for the census")

    verdict = classify(company, tender)

    assert verdict["tier"] == 3
    assert "data processing" in verdict["matched_terms"]


def test_dms_matches_a_document_management_system():
    company = _company(understanding=["DMS"])
    tender = _tender("Document management system implementation")

    verdict = classify(company, tender)

    assert verdict["tier"] == 2
    assert verdict["bucket"] == "strong"


# -- the HTTP list agrees with the gate -----------------------------------


def _store(title, *, authority=None, days=30):
    with SessionLocal() as session:
        tender = TenderRepository(session).create(
            {
                "dedupe_key": f"test:{uuid.uuid4()}",
                "portal_tender_id": f"2027_T_{uuid.uuid4().hex[:8]}_1",
                "work_title": title,
                "authority": authority,
                "organisation_chain": authority,
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


def test_the_list_hides_what_the_gate_rejects_and_says_why(client, fake_email):
    owner = verify(client, fake_email, email="bucket@example.com")
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["wire scanning"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text

    relevant = _store("Wire scanning of the Ganga bridge")
    irrelevant = _store(
        "Housekeeping of a hospital",
        authority="Public Works Department (Scanning Division)",
    )

    listed = client.get("/api/tenders", headers=headers)
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    ids = [item["id"] for item in payload["items"]]

    assert ids == [relevant]
    assert irrelevant not in ids
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["relevance"]["bucket"] == "strong"
    assert item["relevance"]["tier"] == 3
    assert "wire scanning" in item["relevance"]["match_reasons"][0]
