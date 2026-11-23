from __future__ import annotations

from datetime import datetime, timezone

from app.services.corpus_rules import (
    document_key,
    excerpt_hash,
    is_navigation_excerpt,
    map_category,
    map_mandatory,
    normalize_for_match,
    parse_deadline_at,
    tender_dedupe_key,
)
from app.services.pdf_extraction import extract_pdf_pages
from app.services.requirement_service import chunk_pages
from tests.conftest import SAMPLE_PAGES, make_pdf


def test_extract_pdf_pages_keeps_page_provenance(sample_pdf: bytes):
    extraction = extract_pdf_pages(sample_pdf)
    assert extraction.page_count == 3
    assert extraction.text_page_count == 3
    assert extraction.status == "extracted"
    assert extraction.truncated is False
    assert [page.page_index for page in extraction.pages] == [1, 2, 3]
    assert "average annual turnover" in extraction.pages[0].text
    assert extraction.pages[0].printed_label == "1"
    assert extraction.pages[0].text_source == "text_layer"


def test_scanned_pdf_is_not_treated_as_text():
    extraction = extract_pdf_pages(make_pdf([" "]))
    assert extraction.text_page_count == 0
    assert extraction.status == "no_text_layer"
    assert extraction.pages[0].text_source == "empty"


def test_ocr_fallback_labels_the_page_as_ocr():
    recognized = (
        "The bidder shall have an average annual turnover of at least INR 5 crore "
        "during the last three financial years."
    )
    calls: list[bytes] = []

    def fake_ocr(png: bytes) -> str:
        calls.append(png)
        return recognized

    extraction = extract_pdf_pages(make_pdf([" "]), ocr=fake_ocr)
    assert calls
    assert extraction.ocr_page_count == 1
    assert extraction.pages[0].text_source == "ocr"
    assert extraction.pages[0].has_text is True
    assert extraction.pages[0].text == recognized


def test_ocr_is_bounded_by_page_count():
    recognized = "A" * 100
    extraction = extract_pdf_pages(
        make_pdf([" ", " ", " "]), ocr=lambda _png: recognized, ocr_max_pages=1
    )
    assert extraction.ocr_page_count == 1
    assert extraction.ocr_skipped_pages == 2
    assert extraction.text_page_count == 1


def test_normalize_and_hash_ignore_typography_and_whitespace():
    a = "The bidder\u2019s turnover is   INR 5 crore."
    b = "The bidder's turnover is INR 5 crore."
    assert normalize_for_match(a) == normalize_for_match(b)
    assert excerpt_hash(a) == excerpt_hash(b)
    assert len(excerpt_hash(a)) == 64


def test_chunk_pages_respects_character_and_page_limits():
    pages = [
        {"page_index": 1, "text": "a" * 40, "has_text": True},
        {"page_index": 2, "text": "b" * 40, "has_text": True},
        {"page_index": 3, "text": "c" * 40, "has_text": True},
        {"page_index": 4, "text": "short", "has_text": False},
    ]
    chunks = chunk_pages(pages, max_chars=100, max_pages_per_chunk=2)
    assert [len(chunk.pages) for chunk in chunks] == [2, 1]
    assert all(page["has_text"] for chunk in chunks for page in chunk.pages)


def test_category_and_mandatory_mapping_is_explicit():
    assert map_category("technical") == "eligibility"
    assert map_category("financial") == "eligibility"
    assert map_category("form") == "form"
    assert map_category("performance") == "performance"
    assert map_category("something else") == "compliance"
    assert map_mandatory("yes") == "mandatory"
    assert map_mandatory("no") == "not_mandatory"
    assert map_mandatory("maybe") == "unknown"


def test_navigation_detection_does_not_flag_a_normal_obligation():
    assert is_navigation_excerpt(
        "Section 3 - Eligibility Criteria ..... 12", "Table of contents"
    )
    assert is_navigation_excerpt(
        "Table of Contents\nEligibility 3\nSubmission 5", None
    )
    obligation = (
        "The bidder shall have an average annual turnover of at least INR 5 crore "
        "during the last three financial years."
    )
    assert not is_navigation_excerpt(obligation, obligation)


def test_date_only_deadline_survives_ist_display():
    parsed = parse_deadline_at("2026-11-15")
    assert parsed == datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    assert parse_deadline_at("not a date") is None


def test_identity_rules_are_deterministic_and_date_free():
    first = tender_dedupe_key(
        portal_key="CPPP",
        reference="NHAI/2026/1",
        work_title="Four lane road",
        organisation="NHAI",
    )
    second = tender_dedupe_key(
        portal_key="cppp",
        reference="nhai 2026 1",
        work_title="FOUR LANE ROAD",
        organisation="nhai",
    )
    assert first == second
    assert len(first) == 64
    assert document_key("Tender Document.PDF") == "tender-document"
    assert document_key(None) == "document"
    assert "average annual turnover" in SAMPLE_PAGES[0]
