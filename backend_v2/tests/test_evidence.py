"""Exact, two-sided evidence and the order it is ranked by.

The judge may quote, but it may not paraphrase. These tests pin the quote check
and the deterministic hierarchy that sits on top of it: exact core, semantic
core, adjacent, support, generic and finally no evidence.
"""

from __future__ import annotations

from app.discovery.roles import (
    ROLE_ADJACENT,
    ROLE_CORE,
    ROLE_GENERIC,
    ROLE_SUPPORT,
)
from app.services.evidence import (
    MATCH_ADJACENT,
    MATCH_EXACT_CORE,
    MATCH_GENERIC,
    MATCH_NONE,
    MATCH_RANK,
    MATCH_SEMANTIC_CORE,
    MATCH_SUPPORT,
    EvidenceLine,
    EvidenceRetriever,
)


def _line(role: str | None, text: str) -> EvidenceLine:
    return EvidenceLine(text=text, role=role, source="brief")


def test_the_evidence_hierarchy_is_ordered():
    core_text = "DCS Infra provides document scanning and document digitisation."
    assert EvidenceRetriever.match_type(
        _line(ROLE_CORE, core_text), core_text
    ) == (MATCH_EXACT_CORE, MATCH_RANK[MATCH_EXACT_CORE])
    assert EvidenceRetriever.match_type(
        _line(ROLE_CORE, core_text), "document scanning"
    ) == (MATCH_SEMANTIC_CORE, MATCH_RANK[MATCH_SEMANTIC_CORE])
    assert EvidenceRetriever.match_type(
        _line(ROLE_ADJACENT, "court records scanning"), "court records"
    ) == (MATCH_ADJACENT, MATCH_RANK[MATCH_ADJACENT])
    assert EvidenceRetriever.match_type(
        _line(ROLE_SUPPORT, "Annual technical support for the portal"),
        "technical support",
    ) == (MATCH_SUPPORT, MATCH_RANK[MATCH_SUPPORT])
    assert EvidenceRetriever.match_type(
        _line(ROLE_GENERIC, "Web pages and software"), "software"
    ) == (MATCH_GENERIC, MATCH_RANK[MATCH_GENERIC])
    assert EvidenceRetriever.match_type(None, "") == (
        MATCH_NONE,
        MATCH_RANK[MATCH_NONE],
    )

    ranks = [
        MATCH_RANK[MATCH_EXACT_CORE],
        MATCH_RANK[MATCH_SEMANTIC_CORE],
        MATCH_RANK[MATCH_ADJACENT],
        MATCH_RANK[MATCH_SUPPORT],
        MATCH_RANK[MATCH_GENERIC],
        MATCH_RANK[MATCH_NONE],
    ]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_an_exact_substring_is_found_and_a_paraphrase_is_not():
    lines = [
        {
            "text": "DCS Infra provides document scanning and document digitisation.",
            "role": ROLE_CORE,
            "source": "capability",
            "term": "document scanning",
            "page_index": 1,
            "document_id": "doc-1",
        }
    ]

    found = EvidenceRetriever.find_company_line("document scanning", lines)
    assert found is not None
    assert found.document_id == "doc-1"
    assert found.page_index == 1

    assert EvidenceRetriever.find_company_line("We scan court papers.", lines) is None
    assert EvidenceRetriever.find_company_line("", lines) is None
