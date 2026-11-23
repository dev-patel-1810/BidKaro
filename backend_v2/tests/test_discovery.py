from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.discovery import sessions
from app.discovery.browser import ChallengeMarker, PageSnapshot
from app.discovery.handlers import handle_scrape_portal
from app.discovery.portal_walk import PortalWalker
from app.discovery.service import DiscoveryService, dedupe_key_for
from app.models.corpus import DiscoveryCandidate, Tender, TenderLocator
from app.repositories.work import RunRepository
from app.services.job_types import JobParked
from tests.helpers import auth_header, verify

PROFILE = {
    "products_services": ["road"],
    "sectors": ["roads"],
    "preferences": {
        "keywords": ["road"],
        "sectors": ["roads"],
        "regions": ["Maharashtra"],
    },
    "regions": ["Maharashtra"],
}

NOTICE_TEXT = "\n".join(
    [
        "Tender ID 2027_NHAI_123456_1",
        "Tender Reference Number NHAI/2027/1",
        "Bid Submission End Date 31-Dec-2027 05:00 PM",
        "Published Date 01-Oct-2027",
        "Bid Opening Date 01-Jan-2028 11:00 AM",
        "Location Maharashtra",
        "Tender Value in Rs 12,34,56,789",
        "Bid Validity(Days) 120",
        "Organisation Chain National Highways Authority of India||"
        "Ministry of Road Transport and Highways",
        "Construction of four lane road on NH-48.",
    ]
)


def _notice(**overrides):
    notice = {
        "title": "Construction of four lane road on NH-48",
        "notice_url": "https://etenders.gov.in/eprocure/app?tenderId=A",
        "listing_url": "https://etenders.gov.in/eprocure/app?list=1",
        "page_text": NOTICE_TEXT,
        "authority": "National Highways Authority of India",
        "organisation_chain": "National Highways Authority of India||"
        "Ministry of Road Transport and Highways",
        "tender_id": "2027_NHAI_123456_1",
        "reference_number": "NHAI/2027/1",
        "closing_date": "31-Dec-2027 05:00 PM",
        "publishing_date": "01-Oct-2027",
        "opening_date": "01-Jan-2028 11:00 AM",
        "location": "Maharashtra",
        "value": "12,34,56,789",
        "bid_validity": "120",
        "document_links": [],
        "extracted_evidence": {"authority": "National Highways Authority of India"},
    }
    notice.update(overrides)
    return notice


def _hospital_notice(**overrides):
    text = (
        "Tender ID 2027_MOH_999999_1\n"
        "Tender Reference Number MOH/2027/9\n"
        "Bid Submission End Date 31-Dec-2027 05:00 PM\n"
        "Published Date 01-Oct-2027\n"
        "Location Maharashtra\n"
        "Housekeeping and cleaning of a district hospital.\n"
    )
    notice = _notice(
        title="Housekeeping and cleaning of a district hospital",
        notice_url="https://etenders.gov.in/eprocure/app?tenderId=B",
        page_text=text,
        authority="Public Health Department",
        organisation_chain="Public Health Department||Government of Maharashtra",
        tender_id="2027_MOH_999999_1",
        reference_number="MOH/2027/9",
    )
    notice.update(overrides)
    return notice


def test_dedupe_key_uses_identity_and_never_dates():
    first = dedupe_key_for(
        portal_code="cppp",
        portal_tender_id="2027_NHAI_123456_1",
        reference="NHAI/2027/1",
        work_title="Construction of four lane road",
        organisation="NHAI",
    )
    second = dedupe_key_for(
        portal_code="cppp",
        portal_tender_id="2027_NHAI_123456_1",
        reference="something else",
        work_title="A corrected title",
        organisation="Another authority",
    )
    assert first == second
    # A different portal id is a different tender.
    assert (
        first
        != dedupe_key_for(
            portal_code="cppp",
            portal_tender_id="2027_NHAI_123456_2",
            reference=None,
            work_title=None,
            organisation=None,
        )
    )


def test_ingest_is_idempotent_and_never_empties_a_field():
    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        service = DiscoveryService(session, settings=_settings())
        tender, created, reason = service.ingest_notice(_notice(), run_id=run.id)
        assert reason is None
        assert created is True
        assert tender is not None
        first_id = tender.id
        assert tender.closing_at.date() == date(2027, 12, 31)

        # A second read that omits the value and location must not erase them.
        sparse = _notice(value=None, location=None, closing_date="30-Dec-2027 05:00 PM")
        again, created_again, reason_again = service.ingest_notice(sparse)
        assert reason_again is None
        assert created_again is False
        assert again is not None
        assert again.id == first_id
        assert str(again.value_amount) == "123456789"
        assert again.location == "Maharashtra"
        # A stated closing date replaces the old one.
        assert again.closing_at.date() == date(2027, 12, 30)

        count = session.scalar(select(func.count(Tender.id)))
        assert count == 1
        locator = session.scalar(select(TenderLocator))
        assert locator is not None
        assert locator.tender_id == first_id
        assert locator.portal_tender_id == "2027_NHAI_123456_1"
        assert locator.published_on == date(2027, 10, 1)
        candidates = list(session.scalars(select(DiscoveryCandidate)).all())
        assert candidates
        assert {row.outcome for row in candidates} == {"stored"}


def test_ingest_rejects_a_listing_or_a_missing_closing_date():
    with SessionLocal() as session:
        service = DiscoveryService(session, settings=_settings())
        listing, created, reason = service.ingest_notice(
            _notice(title="Latest tenders in Maharashtra", tender_id=None)
        )
        assert listing is None
        assert created is False
        assert reason and "category" in reason.lower()

        no_date, created, reason = service.ingest_notice(
            _notice(closing_date="", tender_id="2027_NHAI_2")
        )
        assert no_date is None
        assert created is False
        assert reason and "closing" in reason.lower()
        assert session.scalar(select(func.count(Tender.id))) == 0


class FakePage:
    """A page that answers the walk's JavaScript with fixed rows."""

    def __init__(self) -> None:
        self.mode = "directory"
        self.empty_directory = False
        self._url = "https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation"

    @property
    def url(self) -> str:
        if self.mode == "list":
            return "https://etenders.gov.in/eprocure/app?page=FrontEndTenderList"
        if self.mode == "notice":
            return "https://etenders.gov.in/eprocure/app?page=FrontEndViewTender&id=A"
        return self._url

    def evaluate(self, script: str):
        if "DirectLink" in script:
            if self.mode != "directory" or self.empty_directory:
                return []
            return [
                {
                    "cells": ["1", "National Highways Authority of India", "2"],
                    "name": "National Highways Authority of India",
                    "count": "2",
                    "linkId": "DirectLink_1",
                }
            ]
        if "FrontEndViewTender" in script:
            if self.mode != "list":
                return []
            return [
                {
                    "cells": ["1", "", "", "", "", ""],
                    "publishing": "01-Oct-2027",
                    "closing": "31-Dec-2027 05:00 PM",
                    "opening": "01-Jan-2028 11:00 AM",
                    "title": "[Construction of four lane road on NH-48] [NHAI/2027/1]",
                    "chain": "National Highways Authority of India",
                    "linkText": "[Construction of four lane road on NH-48]",
                    "refFromCell": "NHAI/2027/1",
                    "href": "/eprocure/app?page=FrontEndViewTender&id=A",
                },
                {
                    "cells": ["2", "", "", "", "", ""],
                    "publishing": "01-Oct-2025",
                    "closing": "31-Dec-2025 05:00 PM",
                    "opening": "01-Jan-2026 11:00 AM",
                    "title": "[Road repair that has already closed] [NHAI/2025/2]",
                    "chain": "National Highways Authority of India",
                    "linkText": "[Road repair that has already closed]",
                    "refFromCell": "NHAI/2025/2",
                    "href": "/eprocure/app?page=FrontEndViewTender&id=B",
                },
                {
                    "cells": ["3", "", "", "", "", ""],
                    "publishing": "01-Oct-2027",
                    "closing": "31-Dec-2027 05:00 PM",
                    "opening": "01-Jan-2028 11:00 AM",
                    "title": "[Housekeeping of a hospital] [MOH/2027/9]",
                    "chain": "Public Health Department",
                    "linkText": "[Housekeeping of a hospital]",
                    "refFromCell": "MOH/2027/9",
                    "href": "/eprocure/app?page=FrontEndViewTender&id=C",
                },
            ]
        if "FILE =" in script:
            return []
        if "const links" in script:
            return False
        return None

    def click(self, selector: str) -> None:
        if "DirectLink" in selector:
            self.mode = "list"

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def goto(self, url: str, **_kwargs) -> None:
        self.mode = "notice"

    def go_back(self) -> None:
        self.mode = "list"


class FakeDriver:
    headless = False

    def __init__(self) -> None:
        self._page = FakePage()
        self.closed = False
        self.events: list[str] = []

    def begin_work(self) -> None:
        self.events.append("begin")

    def reveal(self) -> None:
        self.events.append("reveal")

    def open(self, url: str) -> PageSnapshot:
        self._page.mode = "directory"
        self._page._url = url
        return self.snapshot()

    def snapshot(self) -> PageSnapshot:
        return PageSnapshot(
            url=self._page.url, title="Notice", text=NOTICE_TEXT, html=""
        )

    def find_challenge(self):
        return None

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nwalk"

    def close(self) -> None:
        self.closed = True


class ChallengeDriver(FakeDriver):
    """A driver that reports the portal's search-form captcha on every page."""

    def find_challenge(self):
        return ChallengeMarker(
            kind="captcha",
            selector="#captchaText",
            message="The portal asks for the characters in the picture.",
        )


def _walker(driver) -> PortalWalker:
    return PortalWalker(
        driver,
        PROFILE,
        directory_url="https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation",
        max_notices=10,
        max_notices_per_org=4,
        today=date(2026, 10, 28),
    )


def test_directory_captcha_does_not_hide_a_readable_organisation_list():
    result = _walker(ChallengeDriver()).walk()

    assert result.challenge is None
    assert len(result.notices) == 1
    assert result.notices[0]["tender_id"] == "2027_NHAI_123456_1"


def test_directory_captcha_with_no_rows_stops_for_a_human():
    driver = ChallengeDriver()
    driver._page.empty_directory = True

    result = _walker(driver).walk()

    assert result.challenge is not None
    assert result.notices == []
    assert "human" in (result.stopped_early or "").lower()


def test_portal_walk_reads_one_notice_and_records_every_rejection():
    driver = FakeDriver()
    walker = PortalWalker(
        driver,
        PROFILE,
        directory_url="https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation",
        max_notices=10,
        max_notices_per_org=4,
        today=date(2026, 10, 28),
    )
    result = walker.walk()

    assert result.rows_seen == 3
    assert len(result.notices) == 1
    assert result.notices[0]["tender_id"] == "2027_NHAI_123456_1"
    outcomes = [(row["kind"], row["outcome"]) for row in result.candidates]
    assert outcomes.count(("tender_candidate", "stored")) == 1
    assert outcomes.count(("tender_candidate", "rejected")) == 2
    assert sum(1 for kind, _ in outcomes if kind == "source") >= 2

    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        metrics = DiscoveryService(session, settings=_settings()).ingest_walk(
            result, run_id=run.id
        )
        assert metrics["stored"] == 1
        assert metrics["rejected"] == 0
        assert session.scalar(select(func.count(Tender.id))) == 1
        candidates = list(session.scalars(select(DiscoveryCandidate)).all())
        assert any(row.outcome == "rejected" for row in candidates)
        assert any(row.kind == "source" for row in candidates)


class NeutralOrganisationPage(FakePage):
    """A directory whose only organisation says nothing about the work.

    The name is deliberately ordinary: it carries no word from the profile and
    no vocabulary the software could recognise. The walk must still visit it,
    because only the row's work decides what is listed.
    """

    def __init__(self, title: str = "[Construction of four lane road on NH-48]") -> None:
        super().__init__()
        self._title = title

    def evaluate(self, script: str):
        if "DirectLink" in script:
            if self.mode != "directory":
                return []
            return [
                {
                    "cells": ["1", "Zilla Parishad Pune", "1"],
                    "name": "Zilla Parishad Pune",
                    "count": "1",
                    "linkId": "DirectLink_1",
                }
            ]
        if "FrontEndViewTender" in script:
            if self.mode != "list":
                return []
            return [
                {
                    "cells": ["1", "", "", "", "", ""],
                    "publishing": "01-Oct-2027",
                    "closing": "31-Dec-2027 05:00 PM",
                    "opening": "01-Jan-2028 11:00 AM",
                    "title": self._title,
                    "chain": "Zilla Parishad Pune",
                    "linkText": self._title,
                    "refFromCell": "ZP/2027/1",
                    "href": "/eprocure/app?page=FrontEndViewTender&id=A",
                }
            ]
        return super().evaluate(script)


def test_an_organisation_whose_name_says_nothing_about_the_work_is_still_walked():
    driver = FakeDriver()
    driver._page = NeutralOrganisationPage()

    result = _walker(driver).walk()

    # Nothing in the name matched the profile, so it is not "considered"...
    assert result.organisations_considered == []
    # ...but it is in the directory, so it is walked, and its row is listed.
    assert result.organisations_seen == 1
    assert len(result.notices) == 1
    assert result.rows_seen == 1


def test_a_neutral_organisation_is_visited_even_when_its_row_is_not_the_work():
    driver = FakeDriver()
    driver._page = NeutralOrganisationPage(
        title="[Housekeeping of a district hospital]"
    )

    result = _walker(driver).walk()

    assert result.organisations_seen == 1
    assert result.notices == []
    assert result.rows_seen == 1
    assert any(
        row["kind"] == "tender_candidate"
        and row["outcome"] == "rejected"
        and "did not match" in (row.get("reason") or "")
        for row in result.candidates
    )


class TerminatedDriver(FakeDriver):
    """A driver whose notice page says the bidding cycle is over."""

    def snapshot(self) -> PageSnapshot:
        return PageSnapshot(
            url=self._page.url,
            title="Notice",
            text="Tender Cancelled corrigendum document",
            html="",
        )


def test_a_terminated_notice_is_not_stored_and_is_reported():
    result = _walker(TerminatedDriver()).walk()

    assert result.notices == []
    assert result.terminated == 1
    assert any(
        row["kind"] == "tender_candidate"
        and row["outcome"] == "rejected"
        and "terminated" in (row.get("reason") or "").lower()
        for row in result.candidates
    )


class FakeWalkContext:
    """The part of JobContext the walk handler uses, without a real worker."""
    def __init__(self, job_id: uuid.UUID, run_id: uuid.UUID) -> None:
        self.job_id = job_id
        self.run_id = run_id
        self.payload: dict = {}
        self.parked: dict | None = None

    def heartbeat(self) -> None:
        return None

    def cancelled(self) -> bool:
        return False

    def park(self, *, kind, context=None, image_path=None, expires_at=None):
        self.parked = {
            "kind": kind,
            "context": context or {},
            "image_path": image_path,
        }
        raise JobParked(uuid.uuid4())


def _profile_company(client, fake_email, email: str) -> None:
    owner = verify(client, fake_email, email=email)
    headers = auth_header(owner["access_token"])
    response = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )
    assert response.status_code == 200, response.text


def test_walk_parks_and_resumes_in_the_same_browser(
    client, fake_email, monkeypatch
):
    _profile_company(client, fake_email, "walk@example.com")

    driver = ChallengeDriver()
    driver._page.empty_directory = True
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda *args, **kwargs: driver
    )
    monkeypatch.setattr(
        "app.discovery.handlers._save_challenge", lambda *args: "captcha.png"
    )

    job_id = uuid.uuid4()
    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        run_id = run.id
        session.commit()

    first = FakeWalkContext(job_id, run_id)
    with pytest.raises(JobParked):
        handle_scrape_portal(first)

    assert first.parked is not None
    assert first.parked["kind"] == "captcha"
    instructions = first.parked["context"]["instructions"]
    assert "Type the characters" in instructions
    assert "never stores it" in instructions
    assert first.parked["image_path"] == "captcha.png"
    assert sessions.get(job_id) is not None
    assert driver.closed is False
    # The window is covered while the walk runs and stays that way: the
    # picture is answered in the app, not in the portal window.
    assert driver.events[0] == "begin"
    assert "reveal" not in driver.events

    # The human answered in the app. The walk restarts from the directory in
    # the same browser session.
    driver._page.empty_directory = False
    with SessionLocal() as session:
        resumed = RunRepository(session).start(kind="scrape_portal")
        resumed_id = resumed.id
        session.commit()

    second = FakeWalkContext(job_id, resumed_id)
    result = handle_scrape_portal(second)

    assert result.state == "done"
    assert second.parked is None
    assert sessions.get(job_id) is None
    assert driver.closed is True
    # The resume put the window back into work mode before reading again.
    assert driver.events.count("begin") == 2
    with SessionLocal() as session:
        assert session.scalar(select(func.count(Tender.id))) == 1


def test_a_second_walk_challenge_is_reported_not_looped(
    client, fake_email, monkeypatch
):
    _profile_company(client, fake_email, "walk2@example.com")

    driver = ChallengeDriver()
    driver._page.empty_directory = True
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda *args, **kwargs: driver
    )
    monkeypatch.setattr(
        "app.discovery.handlers._save_challenge", lambda *args: "captcha.png"
    )

    job_id = uuid.uuid4()
    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        first = FakeWalkContext(job_id, run.id)
        session.commit()
    with pytest.raises(JobParked):
        handle_scrape_portal(first)

    # The page still asks for a human after the answer. The walk reports it
    # rather than parking a second time.
    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        second = FakeWalkContext(job_id, run.id)
        session.commit()
    result = handle_scrape_portal(second)

    assert result.state == "partial"
    assert second.parked is None
    assert "again" in (result.notes or "")
    assert sessions.get(job_id) is None
    assert driver.closed is True


def test_a_headless_walk_reports_the_block_instead_of_parking(
    client, fake_email, monkeypatch
):
    _profile_company(client, fake_email, "walk3@example.com")

    driver = ChallengeDriver()
    driver.headless = True
    driver._page.empty_directory = True
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda *args, **kwargs: driver
    )
    monkeypatch.setattr(
        "app.discovery.handlers._save_challenge", lambda *args: "captcha.png"
    )

    job_id = uuid.uuid4()
    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        context = FakeWalkContext(job_id, run.id)
        session.commit()
    result = handle_scrape_portal(context)

    assert result.state == "partial"
    assert context.parked is None
    assert "headless" in (result.notes or "").lower()
    assert sessions.get(job_id) is None
    assert driver.closed is True


def test_the_sweep_routes_start_status_and_cancel_a_listing_sweep(
    client, fake_email
):
    owner = verify(client, fake_email, email="sweeper@example.com")
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text

    started = client.post("/api/discovery/sweeps", headers=headers)
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["created"] is True
    assert body["job"]["kind"] == "sweep_portal"

    # One sweep per company, like one run: a second press returns the same job.
    again = client.post("/api/discovery/sweeps", headers=headers)
    assert again.status_code == 202, again.text
    assert again.json()["created"] is False
    assert again.json()["job"]["id"] == body["job"]["id"]

    # The sweep is visible on the status the Discover page already reads.
    status = client.get("/api/discovery/status", headers=headers)
    assert status.status_code == 200, status.text
    sweep = status.json()["sweep"]
    assert sweep["last_sweep"]["id"] == body["job"]["id"]
    assert sweep["caps"]["interval_hours"] >= 1

    # One browser at a time: run discovery is refused while a sweep is live.
    blocked = client.post("/api/discovery/runs", headers=headers)
    assert blocked.status_code == 409, blocked.text

    cancelled = client.post(
        f"/api/discovery/sweeps/{body['job']['id']}/cancel", headers=headers
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"


def test_discovery_routes_start_and_cancel_a_global_run(client, fake_email):
    owner = verify(client, fake_email, email="discover@example.com")
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text

    status = client.get("/api/discovery/status", headers=headers)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["enabled"] is True
    assert body["caps"]["max_notices"] >= 1

    started = client.post("/api/discovery/runs", headers=headers)
    assert started.status_code == 202, started.text
    first = started.json()
    assert first["created"] is True
    assert first["job"]["kind"] == "scrape_portal"

    # One run at a time: a second press returns the same live job.
    again = client.post("/api/discovery/runs", headers=headers)
    assert again.status_code == 202, again.text
    assert again.json()["created"] is False
    assert again.json()["job"]["id"] == first["job"]["id"]

    run_id = first["job"]["id"]
    detail = client.get(f"/api/discovery/runs/{run_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["kind"] == "scrape_portal"

    listed = client.get("/api/discovery/runs", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [run_id]

    cancelled = client.post(f"/api/discovery/runs/{run_id}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"

    sources = client.get("/api/discovery/sources", headers=headers)
    assert sources.status_code == 200, sources.text
    assert sources.json()["items"] == []


def test_listing_gate_is_per_company_and_saved_rows_stay_visible(client, fake_email):
    owner = verify(client, fake_email, email="listing@example.com")
    headers = auth_header(owner["access_token"])
    profile = client.patch(
        "/api/company",
        headers=headers,
        json={"services": ["road"], "region": "Maharashtra"},
    )
    assert profile.status_code == 200, profile.text

    with SessionLocal() as session:
        service = DiscoveryService(session, settings=_settings())
        road, _, _ = service.ingest_notice(_notice())
        hospital, _, _ = service.ingest_notice(_hospital_notice())
        assert road is not None and hospital is not None
        road_id = str(road.id)
        hospital_id = str(hospital.id)

    listed = client.get("/api/tenders", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["items"]]
    assert road_id in ids
    assert hospital_id not in ids

    # Saving is an explicit judgement: it stays visible even though the work
    # does not match the profile.
    saved = client.post(f"/api/tenders/{hospital_id}/save", headers=headers)
    assert saved.status_code == 200, saved.text
    listed = client.get("/api/tenders", headers=headers)
    ids = [item["id"] for item in listed.json()["items"]]
    assert hospital_id in ids
    only_saved = client.get("/api/tenders?saved=true", headers=headers)
    assert [item["id"] for item in only_saved.json()["items"]] == [hospital_id]

    dismissed = client.post(f"/api/tenders/{hospital_id}/dismiss", headers=headers)
    assert dismissed.status_code == 200, dismissed.text
    listed = client.get("/api/tenders", headers=headers)
    assert hospital_id not in [item["id"] for item in listed.json()["items"]]
    with_dismissed = client.get(
        "/api/tenders?include_dismissed=true", headers=headers
    )
    assert hospital_id in [item["id"] for item in with_dismissed.json()["items"]]


def _settings():
    from app.core.settings import get_settings

    return get_settings()

# -- finding one known tender again ------------------------------------------


class LookupPage:
    """A directory whose lists hold fixed rows, grouped by organisation."""

    def __init__(self, by_org: dict[str, list[dict]], organisations: list[dict] | None = None):
        self.mode = "directory"
        self.by_org = by_org
        self.current = ""
        self.opened: list[str] = []
        self.organisations = organisations or [
            {
                "cells": ["1", "National Highways Authority of India", "1"],
                "name": "National Highways Authority of India",
                "count": "1",
                "linkId": "DirectLink_1",
            }
        ]

    @property
    def url(self) -> str:
        if self.mode == "list":
            return f"https://etenders.gov.in/eprocure/app?list={self.current}"
        if self.mode == "notice":
            return "https://etenders.gov.in/eprocure/app?page=FrontEndViewTender"
        return "https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation"

    def evaluate(self, script: str):
        if "DirectLink" in script:
            return self.organisations if self.mode == "directory" else []
        if "FrontEndViewTender" in script:
            return self.by_org.get(self.current, []) if self.mode == "list" else []
        if "FILE =" in script:
            return []
        if "const links" in script:
            return False
        return None

    def click(self, selector: str) -> None:
        if "DirectLink" in selector:
            self.current = selector.split("_")[-1]
            self.mode = "list"

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def goto(self, url: str, **_kwargs) -> None:
        self.opened.append(url)
        self.mode = "notice"

    def go_back(self) -> None:
        self.mode = "list"


class LookupDriver:
    headless = False

    def __init__(self, page: LookupPage) -> None:
        self._page = page

    def open(self, _url: str) -> PageSnapshot:
        self._page.mode = "directory"
        return self.snapshot()

    def snapshot(self) -> PageSnapshot:
        return PageSnapshot(url=self._page.url, title="Notice", text=NOTICE_TEXT, html="")

    def find_challenge(self):
        return None


def _row(row_id: str, ref: str, title: str, portal_id: str | None = None) -> dict:
    href = f"/eprocure/app?page=FrontEndViewTender&id={row_id}"
    if portal_id:
        href += f"&tenderId={portal_id}"
    return {
        "cells": ["1", "", "", "", "", ""],
        "publishing": "01-Oct-2027",
        "closing": "31-Dec-2027 05:00 PM",
        "opening": "01-Jan-2028 11:00 AM",
        "title": f"[{title}] [{ref}]",
        "chain": "National Highways Authority of India",
        "linkText": f"[{title}]",
        "refFromCell": ref,
        "href": href,
    }


def _lookup_walker(driver) -> PortalWalker:
    return PortalWalker(
        driver,
        PROFILE,
        directory_url="https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation",
        today=date(2026, 10, 28),
    )


def test_a_known_tender_is_found_by_its_unique_id_before_its_title():
    id_row = _row(
        "Z",
        "NHAI/2027/1",
        "A different work",
        portal_id="2027_NHAI_123456_1",
    )
    title_row = _row("A", "NHAI/2025/2", "Construction of four lane road on NH-48")
    page = LookupPage({"1": [title_row, id_row]})
    walker = _lookup_walker(LookupDriver(page))

    notice = walker.open_tender(
        portal_tender_id="2027_NHAI_123456_1",
        reference=None,
        organisation_chain="National Highways Authority of India",
        work_title="Construction of four lane road on NH-48",
    )

    assert notice is not None
    # The title matches an earlier row, but the unique id wins.
    assert "id=Z" in page.opened[-1]


def test_a_known_tender_is_found_by_its_reference_alone():
    rows = [
        _row("A", "OTHER/1", "Construction of four lane road on NH-48"),
        _row("Z", "NHAI/2027/1", "A different work"),
    ]
    page = LookupPage({"1": rows})
    walker = _lookup_walker(LookupDriver(page))

    notice = walker.open_tender(
        portal_tender_id=None,
        reference="NHAI/2027/1",
        organisation_chain="National Highways Authority of India",
        work_title=None,
    )

    assert notice is not None
    assert page.opened[-1].endswith("id=Z")


def test_a_tender_without_a_readable_identifier_is_found_by_its_title():
    rows = [
        _row("A", "OTHER/1", "Housekeeping of a hospital"),
        _row("Z", "OTHER/2", "A different work"),
    ]
    page = LookupPage({"1": rows})
    walker = _lookup_walker(LookupDriver(page))

    notice = walker.open_tender(
        portal_tender_id=None,
        reference=None,
        organisation_chain="National Highways Authority of India",
        work_title="A different work",
    )

    assert notice is not None
    assert page.opened[-1].endswith("id=Z")


def test_the_whole_directory_is_the_final_fallback():
    organisations = [
        {
            "cells": ["1", "National Highways Authority of India", "1"],
            "name": "National Highways Authority of India",
            "count": "1",
            "linkId": "DirectLink_1",
        },
        {
            "cells": ["2", "Maharashtra Road Development Corporation", "1"],
            "name": "Maharashtra Road Development Corporation",
            "count": "1",
            "linkId": "DirectLink_2",
        },
    ]
    page = LookupPage(
        {
            "1": [_row("A", "OTHER/1", "Housekeeping of a hospital")],
            "2": [
                _row(
                    "Z",
                    "NHAI/2027/1",
                    "A different work",
                    portal_id="2027_NHAI_123456_1",
                )
            ],
        },
        organisations=organisations,
    )
    walker = _lookup_walker(LookupDriver(page))

    notice = walker.open_tender(
        portal_tender_id="2027_NHAI_123456_1",
        reference=None,
        organisation_chain="National Highways Authority of India",
        work_title=None,
    )

    assert notice is not None
    # The named organisation was tried first, then the rest of the directory.
    assert "id=Z" in page.opened[-1]
