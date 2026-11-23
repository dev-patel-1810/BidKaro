from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.discovery.browser import PageSnapshot
from app.discovery.handlers import handle_refresh_tender
from app.models.workflow import TenderSnapshot
from app.services.jobs import JobService
from tests.helpers import auth_header, verify
from tests.test_document_fetch import NOTICE_TEXT, _create_tender


class FakeRefreshPage:
    def __init__(self) -> None:
        self.url = "https://example.test/eprocure/app"
        self.clicked: list[str] = []

    def evaluate(self, script: str):
        if "DirectLink" in script:
            return [
                {
                    "name": "National Highways Authority of India",
                    "linkId": "DirectLink_1",
                    "cells": ["1", "National Highways Authority of India", "1"],
                }
            ]
        if "table.list_table" in script:
            return [
                {
                    "linkText": (
                        "[Construction of four lane road on NH-48] "
                        "[NHAI/2027/55][2027_NHAI_555555_1]"
                    ),
                    "title": "Construction of four lane road on NH-48",
                    "refFromCell": "NHAI/2027/55",
                    "href": "/notice/555555",
                    "closing": "31-Dec-2027 05:00 PM",
                    "chain": (
                        "National Highways Authority of India||"
                        "Ministry of Road Transport and Highways"
                    ),
                    "publishing": "01-Oct-2027",
                    "opening": "",
                }
            ]
        if "const results" in script and "FILE" in script:
            return [
                {
                    "url": "https://example.test/files/TenderNotice.pdf",
                    "label": "Tender Notice",
                    "kind": "pdf",
                }
            ]
        return False

    def click(self, selector: str) -> None:
        self.clicked.append(selector)

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass

    def go_back(self) -> None:
        pass


class FakeRefreshDriver:
    def __init__(self) -> None:
        self._page = FakeRefreshPage()
        self.closed = False
        self.covered = False

    def open(self, url: str) -> PageSnapshot:
        self._page.url = url
        return self.snapshot()

    def snapshot(self) -> PageSnapshot:
        return PageSnapshot(
            url=self._page.url,
            title="Notice",
            text=NOTICE_TEXT,
            html="<html><body>notice</body></html>",
        )

    def find_challenge(self):
        return None

    def begin_work(self) -> None:
        self.covered = True

    def close(self) -> None:
        self.closed = True


class TerminatedRefreshDriver(FakeRefreshDriver):
    """A driver whose recheck page says the bidding cycle is over."""

    def snapshot(self) -> PageSnapshot:
        return PageSnapshot(
            url=self._page.url,
            title="Notice",
            text="Tender Cancelled corrigendum document",
            html="<html><body>cancelled</body></html>",
        )


def _snapshot_count(tender_id) -> int:
    with SessionLocal() as session:
        return int(
            session.scalar(
                select(func.count(TenderSnapshot.id)).where(
                    TenderSnapshot.tender_id == tender_id
                )
            )
            or 0
        )


def test_refresh_route_queues_one_source_recheck(client, fake_email):
    owner = verify(client, fake_email, email="refresh@example.com")
    headers = auth_header(owner["access_token"])
    tender_id = _create_tender()

    first = client.post(f"/api/tenders/{tender_id}/refresh", headers=headers)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["created"] is True
    assert body["job"]["kind"] == "refresh_tender"
    assert body["job"]["company_id"] is None

    again = client.post(f"/api/tenders/{tender_id}/refresh", headers=headers)
    assert again.status_code == 202, again.text
    assert again.json()["created"] is False
    assert again.json()["job"]["id"] == body["job"]["id"]


def test_refresh_handler_rereads_by_locator_and_captures_a_snapshot(monkeypatch):
    tender_id = _create_tender()
    before = _snapshot_count(tender_id)
    driver = FakeRefreshDriver()
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda _settings: driver
    )
    context = SimpleNamespace(
        payload={"tender_id": str(tender_id)},
        run_id=None,
        heartbeat=lambda: None,
    )

    result = handle_refresh_tender(context)

    assert result.state == "done", result.notes
    assert result.metrics["same_tender"] is True
    assert result.metrics["created"] is False
    assert driver.closed is True
    assert driver.covered is True  # the recheck never took the screen
    assert _snapshot_count(tender_id) == before + 1

    with SessionLocal() as session:
        from app.models.corpus import Tender, TenderLocator

        tender = session.get(Tender, tender_id)
        assert tender is not None
        locator = session.query(TenderLocator).filter_by(tender_id=tender_id).one()
        assert locator.last_verified_at is not None


def test_refresh_marks_a_terminated_bidding_cycle(monkeypatch):
    tender_id = _create_tender()
    before = _snapshot_count(tender_id)
    driver = TerminatedRefreshDriver()
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda _settings: driver
    )
    context = SimpleNamespace(
        payload={"tender_id": str(tender_id)},
        run_id=None,
        heartbeat=lambda: None,
    )

    result = handle_refresh_tender(context)

    assert result.state == "done", result.notes
    assert result.metrics["cycle_state"] == "terminated"
    assert "bidding cycle is terminated" in result.notes
    assert driver.closed is True
    assert _snapshot_count(tender_id) == before  # nothing new to snapshot

    with SessionLocal() as session:
        from app.models.corpus import Tender
        from app.schemas.corpus import TenderOut
        from app.services.tender_service import TenderService

        tender = session.get(Tender, tender_id)
        assert tender is not None
        assert tender.cycle_state == "terminated"
        assert "cancelled" in (tender.termination_excerpt or "").lower()
        assert tender.termination_observed_at is not None
        assert TenderOut.model_validate(TenderService(session).out(tender)).cycle_state == "terminated"

