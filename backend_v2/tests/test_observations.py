"""The observation trail: every sighting, including the ones that store nothing.

A run that read a notice and rejected it must be distinguishable from a run
that found nothing, so the outcome and the reason are written down. The route
key is built from durable fields, because a portal notice address dies with the
browser session that opened it.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.db import SessionLocal
from app.discovery.service import DiscoveryService
from app.models.intelligence import TenderObservation
from app.models.corpus import Tender
from app.repositories.work import RunRepository

NOTICE_TEXT = "\n".join(
    [
        "Tender ID 2027_NHAI_123456_1",
        "Tender Reference Number NHAI/2027/1",
        "Bid Submission End Date 31-Dec-2027 05:00 PM",
        "Published Date 01-Oct-2027",
        "Location Maharashtra",
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
        "document_links": [],
    }
    notice.update(overrides)
    return notice


def _observations():
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(TenderObservation).order_by(TenderObservation.observed_at)
            ).all()
        )


def test_a_stored_notice_is_recorded_with_its_tender():
    with SessionLocal() as session:
        run = RunRepository(session).start(kind="scrape_portal")
        service = DiscoveryService(session)
        tender, created, reason = service.ingest_notice(_notice(), run_id=run.id)
        assert created is True
        assert reason is None

    observations = _observations()
    assert len(observations) == 1
    observation = observations[0]
    assert observation.outcome == "stored"
    assert observation.tender_id == tender.id
    assert observation.organisation == "National Highways Authority of India"
    assert observation.portal_tender_id == "2027_NHAI_123456_1"
    assert observation.reference == "NHAI/2027/1"
    # The route key is identity, not the session-bound address.
    assert "2027_NHAI_123456_1" in observation.route_key
    assert "eprocure" not in observation.route_key


def test_a_second_read_is_recorded_as_an_update_not_a_second_tender():
    with SessionLocal() as session:
        service = DiscoveryService(session)
        first, created, _reason = service.ingest_notice(_notice())
        assert created is True
        second, created_again, _reason = service.ingest_notice(
            _notice(title="Construction of four lane road on NH-48 (revised)")
        )
        assert created_again is False
        assert second.id == first.id

    outcomes = [row.outcome for row in _observations()]
    assert outcomes == ["stored", "updated"]
    with SessionLocal() as session:
        assert len(session.scalars(select(Tender)).all()) == 1


def test_a_rejected_notice_is_recorded_with_its_reason():
    with SessionLocal() as session:
        service = DiscoveryService(session)
        tender, created, reason = service.ingest_notice(
            _notice(title="", tender_id=None, reference_number=None)
        )
        assert tender is None
        assert "category or index" in (reason or "")

    observations = _observations()
    assert len(observations) == 1
    assert observations[0].outcome == "rejected"
    assert observations[0].reason
    assert observations[0].tender_id is None


def test_a_closed_tender_is_recorded_as_rejected_not_stored():
    with SessionLocal() as session:
        service = DiscoveryService(session)
        tender, created, _reason = service.ingest_notice(
            _notice(closing_date="31-Dec-2020 05:00 PM")
        )
        assert tender is None

    observation = _observations()[-1]
    assert observation.outcome == "rejected"
    assert "past" in (observation.reason or "")


def test_a_listing_row_is_stored_without_claiming_a_notice_read():
    with SessionLocal() as session:
        tender, created, reason = DiscoveryService(session).ingest_notice(
            _notice(page_text="", notice_url=""),
            extracted_from="listing_table",
        )
        assert created is True, reason
        assert tender is not None
        assert tender.extracted_from == "listing_table"
        # A listing row never claims more than the row said.
        assert not tender.unverified


def test_a_later_listing_row_never_downgrades_a_notice_read():
    with SessionLocal() as session:
        service = DiscoveryService(session)
        first, _, _ = service.ingest_notice(_notice())
        assert first is not None
        second, created, _ = service.ingest_notice(
            _notice(page_text=""), extracted_from="listing_table"
        )
        assert created is False
        assert second is not None
        assert second.extracted_from == "notice_page"
