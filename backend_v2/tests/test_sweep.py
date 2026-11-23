"""The listing sweep: whole directory, every list page, no notice opened.

The sweep is what makes the corpus first and the profile second. These tests
pin the two properties that matter most: it never spends a browser page on a
notice it does not need, and stopping it loses at most the page in flight.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.db import SessionLocal
from app.discovery.browser import PageSnapshot
from app.discovery.sweep import PortalSweeper, organisation_ages
from app.services.job_types import JobCancelled

DIRECTORY = "https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation"
TODAY = date(2026, 11, 16)


def _row(title: str, reference: str, tender_id: str, closing: str = "31-Dec-2027 05:00 PM"):
    return {
        "cells": ["1", "", "", "", "", ""],
        "publishing": "01-Oct-2027",
        "closing": closing,
        "opening": "01-Jan-2028 11:00 AM",
        # The portal writes the work, the reference and the tender id in the
        # same cell; the sweep must read all three without opening the notice.
        "title": f"[{title}] [{reference}][{tender_id}]",
        "chain": "Some Organisation||Government of India",
        "linkText": f"[{title}]",
        "refFromCell": reference,
        "href": f"/eprocure/app?page=FrontEndViewTender&id={tender_id}",
    }


class SweepFakePage:
    """A directory with two organisations; one of them has two list pages."""

    def __init__(self) -> None:
        self.mode = "directory"
        self.organisation: str | None = None
        self.page_number = 1
        self.clicks: list[str] = []
        self.opened_urls: list[str] = []
        self.goto_calls: list[str] = []
        self._url = DIRECTORY
        self.pages = {
            "National Highways Authority of India": {
                1: [_row("Road resurfacing of NH-48", "NHAI/2027/1", "2027_NHAI_1_1")],
                2: [_row("Road survey of NH-48", "NHAI/2027/2", "2027_NHAI_2_1")],
            },
            "Public Works Department": {
                1: [_row("Housekeeping of a hospital", "PWD/2027/9", "2027_PWD_9_1")],
            },
        }

    @property
    def url(self) -> str:
        if self.mode == "list":
            return f"{DIRECTORY}&org={self.organisation}&page={self.page_number}"
        return self._url

    def evaluate(self, script: str):
        if "DirectLink" in script:
            if self.mode != "directory":
                return []
            return [
                {
                    "cells": ["1", "National Highways Authority of India", "2"],
                    "name": "National Highways Authority of India",
                    "count": "2",
                    "linkId": "DirectLink_1",
                },
                {
                    "cells": ["2", "Public Works Department", "1"],
                    "name": "Public Works Department",
                    "count": "1",
                    "linkId": "DirectLink_2",
                },
            ]
        if "FrontEndViewTender" in script:
            if self.mode != "list" or self.organisation is None:
                return []
            return list(self.pages.get(self.organisation, {}).get(self.page_number, []))
        if "const links" in script:
            # "Next page": a list with two pages, or a directory with one.
            if self.mode == "list" and self.organisation is not None:
                if self.page_number < len(self.pages.get(self.organisation, {})):
                    self.page_number += 1
                    return True
            return False
        return None

    def click(self, selector: str) -> None:
        if selector.startswith("#DirectLink"):
            self.clicks.append(selector)
            index = selector.rsplit("_", 1)[-1]
            self.organisation = {
                "1": "National Highways Authority of India",
                "2": "Public Works Department",
            }[index]
            self.mode = "list"
            self.page_number = 1

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)


class SweepFakeDriver:
    headless = True

    def __init__(self) -> None:
        self._page = SweepFakePage()
        self.closed = False
        self.revealed = False

    def begin_work(self) -> None:
        return None

    def reveal(self) -> None:
        self.revealed = True

    def open(self, url: str) -> PageSnapshot:
        self._page.mode = "directory"
        self._page.organisation = None
        self._page.page_number = 1
        self._page._url = url
        return self.snapshot()

    def snapshot(self) -> PageSnapshot:
        return PageSnapshot(url=self._page.url, title="Directory", text="", html="")

    def find_challenge(self):
        return None

    def screenshot(self):
        return b""

    def close(self) -> None:
        self.closed = True


class Sink:
    """Collects the pages the sweep hands over."""

    def __init__(self, *, stop_after: int | None = None) -> None:
        self.pages: list = []
        self.stop_after = stop_after

    def __call__(self, page) -> None:
        self.pages.append(page)
        if self.stop_after is not None and len(self.pages) >= self.stop_after:
            raise JobCancelled()


def _sweeper(driver, **overrides) -> PortalSweeper:
    options = {
        "directory_url": DIRECTORY,
        "max_pages": 5,
        "max_org_pages": 4,
        "today": TODAY,
    }
    options.update(overrides)
    return PortalSweeper(driver, **options)


def test_the_sweep_reads_every_list_page_and_opens_no_notice():
    driver = SweepFakeDriver()
    sink = Sink()

    result = _sweeper(driver).sweep(sink)

    assert result.organisations_total == 2
    assert result.organisations_walked == 2
    assert result.pages == 3  # two pages for NHAI, one for PWD
    assert result.rows_seen == 3
    assert result.stopped_early is None
    # The whole point: a listing table is enough, so no notice page is loaded.
    assert driver._page.goto_calls == []
    assert driver.revealed is False

    notices = [notice for page in sink.pages for notice in page.notices]
    assert len(notices) == 3
    first = notices[0]
    assert first["title"] == "Road resurfacing of NH-48"
    assert first["tender_id"] == "2027_NHAI_1_1"
    assert first["reference_number"] == "NHAI/2027/1"
    assert first["authority"] == "National Highways Authority of India"
    assert first["page_text"] == ""
    assert first["sweep"]["list_page"] == 1


def test_cancelling_keeps_the_pages_already_handed_over():
    driver = SweepFakeDriver()
    sink = Sink(stop_after=1)

    with pytest.raises(JobCancelled):
        _sweeper(driver).sweep(sink)

    # One page was stored before the stop, and the sweep did not open the
    # second organisation to find out.
    assert len(sink.pages) == 1
    assert driver._page.clicks == ["#DirectLink_1"]


def test_the_stalest_organisation_is_read_first():
    driver = SweepFakeDriver()
    ages = {
        "National Highways Authority of India": datetime(2026, 11, 16, tzinfo=timezone.utc),
        "Public Works Department": datetime(2026, 11, 1, tzinfo=timezone.utc),
    }

    _sweeper(driver, organisation_age=ages).sweep(Sink())

    assert driver._page.clicks == ["#DirectLink_2", "#DirectLink_1"]


def test_an_organisation_never_seen_comes_before_one_seen_recently():
    driver = SweepFakeDriver()
    ages = {
        "National Highways Authority of India": datetime(2026, 11, 16, tzinfo=timezone.utc),
    }

    _sweeper(driver, organisation_age=ages).sweep(Sink())

    assert driver._page.clicks == ["#DirectLink_2", "#DirectLink_1"]


def test_a_bounded_sweep_stops_at_the_cap():
    driver = SweepFakeDriver()

    result = _sweeper(driver, max_organisations=1).sweep(Sink())

    assert result.organisations_total == 2
    assert result.organisations_walked == 1
    assert driver._page.clicks == ["#DirectLink_1"]


def test_a_page_cap_does_not_lose_the_first_page():
    driver = SweepFakeDriver()
    sink = Sink()

    result = _sweeper(driver, max_pages_per_org=1).sweep(sink)

    assert result.pages == 2
    notices = [notice for page in sink.pages for notice in page.notices]
    # The first page of each organisation is read, and the pager is not used.
    assert [notice["title"] for notice in notices] == [
        "Road resurfacing of NH-48",
        "Housekeeping of a hospital",
    ]


def test_organisation_ages_reads_the_observation_trail():
    from app.repositories.intelligence import ObservationRepository

    with SessionLocal() as session:
        ObservationRepository(session).record(
            route_key="organisation-a | 2027_A_1_1",
            outcome="stored",
            organisation="Organisation A",
        )
        ObservationRepository(session).record(
            route_key="organisation-b | 2027_B_1_1",
            outcome="stored",
            organisation="Organisation B",
        )
        session.commit()
        ages = organisation_ages(session)

    assert set(ages) == {"Organisation A", "Organisation B"}
    assert all(isinstance(value, datetime) for value in ages.values())
    # Newer sightings are newer.
    assert ages["Organisation B"] >= ages["Organisation A"]


def test_the_status_says_what_the_last_sweep_did():
    from app.discovery.service import SWEEP_KIND, DiscoveryService
    from app.repositories.work import RunRepository
    from app.services.jobs import JobService

    with SessionLocal() as session:
        job, created = JobService(session).enqueue(kind=SWEEP_KIND, lane="browser")
        assert created is True
        run = RunRepository(session).start(kind=SWEEP_KIND, job_id=job.id)
        RunRepository(session).finish(
            run,
            status="done",
            metrics={"pages": 3, "rows_seen": 41, "stored": 12},
            notes="Read 3 list page(s) from 2 organisation(s).",
        )
        session.commit()
        status = DiscoveryService(session).sweep_status()

    # A sweep reports counts, not just a state: the page can say what the run
    # did without anyone opening a log.
    assert status["last_sweep"].id == job.id
    assert status["last_summary"]["state"] == "done"
    assert status["last_summary"]["metrics"]["pages"] == 3
    assert status["last_summary"]["metrics"]["stored"] == 12
    assert "3 list page(s)" in status["last_summary"]["notes"]
