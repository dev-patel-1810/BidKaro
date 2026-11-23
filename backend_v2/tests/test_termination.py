from __future__ import annotations

from app.discovery.tender_extraction import TERMINATION_MESSAGE, termination_statement


def test_the_portals_cancelled_tender_status_is_a_termination_statement():
    statement = termination_statement("Tender Cancelled corrigendum document")

    assert statement == "Tender Cancelled corrigendum document"


def test_the_terminated_cycle_sentence_is_read_verbatim():
    text = (
        "The current bidding cycle is terminated. "
        "A completely new process will be initiated."
    )

    statement = termination_statement(text)

    assert statement is not None
    assert "bidding cycle is terminated" in statement


def test_the_navigation_link_alone_is_not_a_statement():
    # "Cancelled/Retendered" is a navigation link on every CPPP page. It says
    # nothing about this tender and must never mark one terminated.
    assert termination_statement("Home | Search | Cancelled/Retendered | Site Map") is None


def test_a_result_notice_is_not_a_termination_statement():
    assert termination_statement("Tender awarded to the successful bidder") is None


def test_the_message_is_fixed_and_does_not_invent_a_new_date():
    assert "completely new process" in TERMINATION_MESSAGE
    assert "2026" not in TERMINATION_MESSAGE
