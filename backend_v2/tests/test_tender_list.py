from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.core.db import SessionLocal
from app.discovery.relevance import profile_is_complete, tender_matches_company
from app.repositories.corpus import TenderRepository
from tests.helpers import auth_header, verify

#: Relative to the real clock so the closed-tender filter never turns a fixture
#: that is meant to be open into a closed one.
NOW = datetime.now(timezone.utc)


def _tender(
    *,
    title: str,
    reference: str | None = None,
    authority: str | None = None,
    closing_in_days: int | None = 30,
    created_minutes_ago: int = 0,
    source_type: str = "official_portal",
    organisation_chain: str | None = None,
    location: str | None = "Maharashtra",
):
    """Store one corpus tender with an exact arrival and closing time."""

    closing_at = (
        None
        if closing_in_days is None
        else NOW + timedelta(days=closing_in_days)
    )
    with SessionLocal() as session:
        tender = TenderRepository(session).create(
            {
                "dedupe_key": f"test:{uuid.uuid4()}",
                "portal_tender_id": f"2027_T_{uuid.uuid4().hex[:8]}_1",
                "work_title": title,
                "reference": reference,
                "authority": authority,
                "organisation_chain": organisation_chain or authority,
                "location": location,
                "region": location,
                "closing_at": closing_at,
                "source_type": source_type,
                "extracted_from": "notice_page",
                "created_at": NOW - timedelta(minutes=created_minutes_ago),
            }
        )
        session.commit()
        return str(tender.id)


def _road_company(client, fake_email, *, email: str = "list@example.com") -> dict:
    owner = verify(client, fake_email, email=email)
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text
    return headers


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_the_default_order_is_newest_arrival_first(client, fake_email):
    headers = _road_company(client, fake_email)
    oldest = _tender(title="Road work A", created_minutes_ago=30)
    newest = _tender(title="Road work C", created_minutes_ago=1)
    middle = _tender(title="Road work B", created_minutes_ago=15)

    listed = client.get("/api/tenders", headers=headers)

    # Arrival order, not closing order: the newest read comes first even
    # though a different row closes sooner.
    assert _ids(listed) == [newest, middle, oldest]


def test_closing_order_is_the_readers_choice_and_nulls_go_last(client, fake_email):
    headers = _road_company(client, fake_email)
    late = _tender(title="Road work late", closing_in_days=40, created_minutes_ago=1)
    soon = _tender(title="Road work soon", closing_in_days=5, created_minutes_ago=2)
    no_date = _tender(
        title="Road work undated", closing_in_days=None, created_minutes_ago=3
    )

    soonest = client.get("/api/tenders?sort=closing_asc", headers=headers)
    assert _ids(soonest) == [soon, late, no_date]

    farthest = client.get("/api/tenders?sort=closing_desc", headers=headers)
    assert _ids(farthest) == [late, soon, no_date]

    # Oldest arrival first, whatever the deadlines say.
    oldest = client.get("/api/tenders?sort=created_asc", headers=headers)
    assert _ids(oldest) == [no_date, soon, late]


def test_an_unknown_sort_is_rejected_instead_of_guessed(client, fake_email):
    headers = _road_company(client, fake_email)

    response = client.get("/api/tenders?sort=relevance", headers=headers)

    assert response.status_code == 422, response.text


def test_a_page_reports_the_honest_total_and_page_count(client, fake_email):
    headers = _road_company(client, fake_email)
    for index in range(3):
        _tender(title=f"Road work {index}", created_minutes_ago=index + 1)

    first = client.get("/api/tenders?limit=2", headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["pages"] == 2
    assert body["next"] == "2"

    second = client.get("/api/tenders?limit=2&offset=2", headers=headers)
    body = second.json()
    assert len(body["items"]) == 1
    assert body["total"] == 3
    assert body["page"] == 2
    assert body["pages"] == 2
    assert body["next"] is None


def test_search_runs_on_the_server_and_keeps_percent_literal(client, fake_email):
    headers = _road_company(client, fake_email)
    flagged = _tender(
        title="Supply of 100% cotton uniforms for road maintenance",
        reference="UNI/2027/1",
        authority="Zilla Parishad Pune",
    )
    road = _tender(
        title="Construction of four lane road",
        reference="NHAI/2027/7",
        authority="National Highways Authority of India",
    )

    by_title = client.get("/api/tenders", params={"q": "cotton"}, headers=headers)
    assert _ids(by_title) == [flagged]

    by_reference = client.get(
        "/api/tenders", params={"q": "NHAI/2027/7"}, headers=headers
    )
    assert _ids(by_reference) == [road]

    by_authority = client.get("/api/tenders", params={"q": "zilla"}, headers=headers)
    assert _ids(by_authority) == [flagged]

    # A '%' is a character the user typed, not a wildcard: this must not
    # match every row.
    literal = client.get("/api/tenders", params={"q": "100%"}, headers=headers)
    assert _ids(literal) == [flagged]
    assert literal.json()["total"] == 1

    empty = client.get("/api/tenders", params={"q": "nothing-here"}, headers=headers)
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0


def test_mine_is_saved_and_bidding_only(client, fake_email):
    headers = _road_company(client, fake_email)
    saved = _tender(title="Road work saved")
    bidding = _tender(title="Road work bidding")
    untouched = _tender(title="Road work untouched")

    assert client.post(f"/api/tenders/{saved}/save", headers=headers).status_code == 200
    assert (
        client.post(f"/api/tenders/{bidding}/bidding", headers=headers).status_code
        == 200
    )

    mine = client.get("/api/tenders?mine=true", headers=headers)

    assert set(_ids(mine)) == {saved, bidding}
    assert mine.json()["total"] == 2
    assert untouched not in _ids(mine)


def test_an_empty_profile_shows_the_corpus_and_says_the_profile_is_incomplete(
    client, fake_email
):
    owner = verify(client, fake_email, email="bare@example.com")
    headers = auth_header(owner["access_token"])
    region_only = client.patch(
        "/api/company", headers=headers, json={"region": "Maharashtra"}
    )
    assert region_only.status_code == 200, region_only.text

    hospital = _tender(title="Housekeeping of a district hospital")
    road = _tender(title="Construction of four lane road")

    listed = client.get("/api/tenders", headers=headers)

    # A region is not a work profile: the corpus is shown under an "all
    # tenders" heading instead of pretending both rows match the company.
    assert set(_ids(listed)) == {hospital, road}
    assert listed.json()["profile_complete"] is False


def test_a_declared_work_profile_is_complete_and_filters_by_work(client, fake_email):
    headers = _road_company(client, fake_email)
    hospital = _tender(title="Housekeeping of a district hospital")
    road = _tender(title="Construction of four lane road")

    listed = client.get("/api/tenders", headers=headers)

    assert _ids(listed) == [road]
    assert listed.json()["profile_complete"] is True
    assert hospital not in _ids(listed)


def test_show_everything_brings_back_the_rows_the_gate_hid(client, fake_email):
    headers = _road_company(client, fake_email)
    hospital = _tender(title="Housekeeping of a district hospital")
    road = _tender(title="Construction of four lane road")

    default = client.get("/api/tenders", headers=headers)
    assert _ids(default) == [road]
    assert default.json()["total"] == 1

    everything = client.get("/api/tenders?include_hidden=true", headers=headers)
    assert set(_ids(everything)) == {road, hospital}
    assert everything.json()["total"] == 2
    # The row comes back labelled, not silently promoted: the bucket is still
    # the gate's answer, so the card can say why it was hidden.
    buckets = {
        item["id"]: item["relevance"]["bucket"]
        for item in everything.json()["items"]
    }
    assert buckets[hospital] == "hidden"
    assert buckets[road] != "hidden"


def test_closed_tenders_are_hidden_unless_saved_or_asked_for(client, fake_email):
    headers = _road_company(client, fake_email, email="closed@example.com")
    open_tender = _tender(title="Road work open", closing_in_days=30)
    closed = _tender(title="Road work closed", closing_in_days=-1)
    undated = _tender(title="Road work undated", closing_in_days=None)

    listed = client.get("/api/tenders", headers=headers)
    # The undated row is unknown, not closed, so it stays visible.
    assert set(_ids(listed)) == {open_tender, undated}

    everything = client.get(
        "/api/tenders", headers=headers, params={"include_closed": True}
    )
    assert set(_ids(everything)) == {open_tender, closed, undated}

    # A saved row is work in progress, so the saved view keeps it even after
    # its deadline has passed.
    saved = client.post(f"/api/tenders/{closed}/save", headers=headers)
    assert saved.status_code == 200, saved.text
    mine = client.get("/api/tenders?mine=true", headers=headers)
    assert closed in _ids(mine)


def test_profile_is_complete_needs_work_not_a_region():
    class Company:
        region = "Maharashtra"
        sectors: list[str] = []
        services: list[str] = []
        keywords: list[str] = []
        profile: dict = {}

    assert profile_is_complete(Company()) is False

    Company.services = ["road"]
    assert profile_is_complete(Company()) is True


def test_an_incomplete_profile_shows_a_tender_the_gate_would_otherwise_hide():
    class Company:
        region = "Maharashtra"
        sectors: list[str] = []
        services: list[str] = []
        keywords: list[str] = []
        profile: dict = {}

    class Tender:
        source_type = "official_portal"
        work_title = "Housekeeping of a district hospital"
        work_type = None
        organisation_chain = "Public Health Department"
        location = "Maharashtra"
        region = "Maharashtra"

    # No work declared: the corpus is shown rather than an empty page. The
    # route flags the profile as incomplete so the UI can ask for one.
    assert tender_matches_company(Company(), Tender()) is True


def test_best_fit_first_puts_the_stronger_fit_above_a_newer_weaker_one(client, fake_email):
    owner = verify(client, fake_email, email="fit@example.com")
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road", "bridge"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text

    # The older row names both declared words; the newer rows name one. Fit
    # outranks arrival, which is the opposite of the newest-first default.
    strong = _tender(
        title="Construction of a road bridge over the river",
        created_minutes_ago=120,
    )
    weak_newer = _tender(title="Road repair work", created_minutes_ago=5)
    weak_older = _tender(title="Road painting work", created_minutes_ago=60)

    best = client.get("/api/tenders?sort=match_desc", headers=headers)
    assert best.status_code == 200, best.text
    ordered = _ids(best)
    assert ordered[0] == strong
    # Equal fits still read newest first, so the order is score then arrival.
    assert ordered[1:] == [weak_newer, weak_older]

    scores = [item["relevance"]["match_score"] for item in best.json()["items"]]
    assert scores == sorted(scores, reverse=True)

    newest = client.get("/api/tenders?sort=created_desc", headers=headers)
    assert _ids(newest) == [weak_newer, weak_older, strong]


def test_removing_a_saved_tender_takes_it_out_of_my_tenders(client, fake_email):
    headers = _road_company(client, fake_email, email="unsave@example.com")
    tender = _tender(title="Road work to unsave")

    saved = client.post(f"/api/tenders/{tender}/save", headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] is True
    assert _ids(client.get("/api/tenders?mine=true", headers=headers)) == [tender]

    removed = client.delete(f"/api/tenders/{tender}/save", headers=headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()["saved"] is False
    assert removed.json()["bidding"] is False

    assert client.get("/api/tenders?mine=true", headers=headers).json()["items"] == []
    # Removing a bookmark is not a rejection: it is still a candidate.
    assert _ids(client.get("/api/tenders", headers=headers)) == [tender]

    # Calling it again is harmless, not a 404.
    assert client.delete(f"/api/tenders/{tender}/save", headers=headers).status_code == 200


def test_removing_a_save_keeps_a_bid_in_preparation(client, fake_email):
    headers = _road_company(client, fake_email, email="bid@example.com")
    tender = _tender(title="Road work with a bid")

    client.post(f"/api/tenders/{tender}/save", headers=headers)
    client.post(f"/api/tenders/{tender}/bidding", headers=headers)

    removed = client.delete(f"/api/tenders/{tender}/save", headers=headers)

    assert removed.status_code == 200, removed.text
    assert removed.json()["bidding"] is True
    # A bid is work in progress; only the bookmark went away.
    assert _ids(client.get("/api/tenders?mine=true", headers=headers)) == [tender]
