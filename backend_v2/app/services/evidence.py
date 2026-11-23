"""Retrieve exact evidence lines for a fit judgment.

This is intentionally not a generative step. The company side is read from the
current company brief (or its deterministic preview) and the tender side from
the notice's own title, work type and cited requirements. Every line keeps its
source and page, and a quote is accepted only when it is found in one of those
stored lines. If either side has nothing, the answer is "no evidence": the code
never fills the gap with a plausible sentence.

Postgres is the source of truth. The retriever is a small lexical ranker over
those rows; a vector index can sit in front of it later without changing the
citation check.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.discovery.roles import (
    ROLE_ADJACENT,
    ROLE_CORE,
    ROLE_GENERIC,
    ROLE_SUPPORT,
    classify_term,
)
from app.discovery.work_text import NOISE_WORDS, normalize_text, term_tokens, tokens
from app.repositories.corpus import RequirementRepository
from app.services.company_brief import CompanyBriefService, payload_terms
from app.services.corpus_rules import normalize_for_match

MATCH_EXACT_CORE = "exact_core"
MATCH_SEMANTIC_CORE = "semantic_core"
MATCH_ADJACENT = "adjacent"
MATCH_SUPPORT = "support_only"
MATCH_GENERIC = "generic_only"
MATCH_NONE = "no_evidence"

MATCH_TYPES = (
    MATCH_EXACT_CORE,
    MATCH_SEMANTIC_CORE,
    MATCH_ADJACENT,
    MATCH_SUPPORT,
    MATCH_GENERIC,
    MATCH_NONE,
)

MATCH_RANK = {
    MATCH_EXACT_CORE: 1,
    MATCH_SEMANTIC_CORE: 2,
    MATCH_ADJACENT: 3,
    MATCH_SUPPORT: 4,
    MATCH_GENERIC: 5,
    MATCH_NONE: 6,
}

ROLE_PRIORITY = {
    ROLE_CORE: 0,
    ROLE_ADJACENT: 1,
    ROLE_SUPPORT: 2,
    ROLE_GENERIC: 3,
}

_STOP = frozenset(NOISE_WORDS) | frozenset(
    {
        "and",
        "are",
        "for",
        "from",
        "has",
        "have",
        "into",
        "its",
        "not",
        "the",
        "this",
        "that",
        "with",
        "work",
        "works",
    }
)


@dataclass(slots=True)
class EvidenceLine:
    text: str
    role: str | None
    source: str
    term: str | None = None
    page_index: int | None = None
    printed_label: str | None = None
    document_id: str | None = None
    reference: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def out(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "role": self.role,
            "source": self.source,
            "term": self.term,
            "page_index": self.page_index,
            "printed_label": self.printed_label,
            "document_id": self.document_id,
            "reference": self.reference,
        }


def _search_tokens(text: str) -> set[str]:
    return {
        token
        for token in tokens(normalize_text(text))
        if len(token) >= 3 and token not in _STOP
    }


def _overlap(text: str, other_tokens: set[str]) -> int:
    return len(_search_tokens(text) & other_tokens)


def _company_lines(payload: dict[str, Any]) -> list[EvidenceLine]:
    lines: list[EvidenceLine] = []
    seen: set[tuple[str, str]] = set()
    for role, key in (
        (ROLE_CORE, "core_terms"),
        (ROLE_ADJACENT, "adjacent_terms"),
        (ROLE_SUPPORT, "support_terms"),
        (ROLE_GENERIC, "generic_terms"),
    ):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term") or "").strip()
            evidence = str(item.get("evidence") or term).strip()
            if not term or not evidence:
                continue
            digest = (normalize_for_match(evidence), role)
            if digest in seen:
                continue
            seen.add(digest)
            lines.append(
                EvidenceLine(
                    text=evidence,
                    role=role,
                    source=str(item.get("source") or "brief"),
                    term=term,
                    page_index=item.get("page_index"),
                    printed_label=item.get("printed_label"),
                    document_id=item.get("document_id"),
                    reference=str(item.get("source_id") or "") or None,
                )
            )
    for project in payload.get("projects") or []:
        if not isinstance(project, dict):
            continue
        evidence = str(project.get("evidence") or "").strip()
        title = str(project.get("title") or "").strip()
        if not evidence or not title:
            continue
        digest = (normalize_for_match(evidence), ROLE_CORE)
        if digest in seen:
            continue
        seen.add(digest)
        lines.append(
            EvidenceLine(
                text=evidence,
                role=ROLE_CORE,
                source="project",
                term=str(project.get("work_family") or title),
                page_index=project.get("page_index"),
                printed_label=project.get("printed_label"),
                document_id=project.get("document_id"),
                reference=str(project.get("source_id") or "") or None,
            )
        )
    return lines


class EvidenceRetriever:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.requirements = RequirementRepository(session)
        self.briefs = CompanyBriefService(session)

    def company_lines(
        self, company, *, payload: dict[str, Any] | None = None
    ) -> list[EvidenceLine]:
        if payload is None:
            payload = self.briefs.current_payload(company.id)
        if payload is None:
            payload = self.briefs.derive_payload(company)
        return _company_lines(payload)

    def tender_lines(self, tender) -> list[EvidenceLine]:
        lines: list[EvidenceLine] = []
        seen: set[str] = set()

        def add(
            text: str | None,
            *,
            source: str,
            reference: str | None = None,
            page_index: int | None = None,
            printed_label: str | None = None,
            document_id: str | None = None,
        ) -> None:
            value = str(text or "").strip()
            if not value:
                return
            digest = normalize_for_match(value)
            if not digest or digest in seen:
                return
            seen.add(digest)
            lines.append(
                EvidenceLine(
                    text=value,
                    role=None,
                    source=source,
                    reference=reference,
                    page_index=page_index,
                    printed_label=printed_label,
                    document_id=document_id,
                )
            )

        add(tender.work_title, source="notice_title", reference=str(tender.id))
        add(tender.work_type, source="work_type", reference=str(tender.id))
        rows, _total = self.requirements.list_for_tender(
            tender.id, include_navigation=False, limit=20
        )
        for row in rows:
            add(
                row.excerpt,
                source="requirement",
                reference=str(row.id),
                page_index=row.page_index,
                printed_label=row.printed_label,
                document_id=str(row.document_id),
            )
        return lines

    def retrieve(
        self,
        company,
        tender,
        *,
        payload: dict[str, Any] | None = None,
        max_company: int = 12,
        max_tender: int = 12,
    ) -> dict[str, Any]:
        company_lines = self.company_lines(company, payload=payload)
        tender_lines = self.tender_lines(tender)
        if not company_lines or not tender_lines:
            return {
                "company_lines": [line.out() for line in company_lines[:max_company]],
                "tender_lines": [line.out() for line in tender_lines[:max_tender]],
                "company_terms": payload_terms(payload),
                "match_type": MATCH_NONE,
                "evidence_rank": MATCH_RANK[MATCH_NONE],
                "reason": "No stored evidence line was available on both sides.",
            }

        company_tokens: set[str] = set()
        for line in company_lines:
            company_tokens |= _search_tokens(f"{line.term or ''} {line.text}")
        tender_tokens: set[str] = set()
        for line in tender_lines:
            tender_tokens |= _search_tokens(line.text)

        def company_key(line: EvidenceLine) -> tuple[int, int, str]:
            return (
                ROLE_PRIORITY.get(str(line.role or ""), 9),
                -_overlap(line.text, tender_tokens),
                line.text.casefold(),
            )

        def tender_key(line: EvidenceLine) -> tuple[int, int, str]:
            order = {"notice_title": 0, "work_type": 1, "requirement": 2}.get(
                line.source, 3
            )
            return (order, -_overlap(line.text, company_tokens), line.text.casefold())

        selected_company = sorted(company_lines, key=company_key)[:max_company]
        selected_tender = sorted(tender_lines, key=tender_key)[:max_tender]
        return {
            "company_lines": [line.out() for line in selected_company],
            "tender_lines": [line.out() for line in selected_tender],
            "company_terms": payload_terms(payload),
            "match_type": None,
            "evidence_rank": None,
            "reason": None,
        }

    @staticmethod
    def find_company_line(
        quote: str, lines: list[dict[str, Any]] | list[EvidenceLine]
    ) -> EvidenceLine | None:
        target = normalize_for_match(quote)
        if not target:
            return None
        for line in lines:
            text = line.text if isinstance(line, EvidenceLine) else str(line.get("text") or "")
            if target and target in normalize_for_match(text):
                if isinstance(line, EvidenceLine):
                    return line
                return EvidenceLine(
                    text=text,
                    role=line.get("role"),
                    source=str(line.get("source") or "brief"),
                    term=line.get("term"),
                    page_index=line.get("page_index"),
                    printed_label=line.get("printed_label"),
                    document_id=line.get("document_id"),
                    reference=line.get("reference"),
                )
        return None

    @staticmethod
    def find_tender_line(
        quote: str, lines: list[dict[str, Any]] | list[EvidenceLine]
    ) -> EvidenceLine | None:
        return EvidenceRetriever.find_company_line(quote, lines)

    @staticmethod
    def match_type(company_line: EvidenceLine | None, quote: str) -> tuple[str, int]:
        if company_line is None:
            return MATCH_NONE, MATCH_RANK[MATCH_NONE]
        role = str(company_line.role or classify_term(company_line.term or company_line.text))
        if role == ROLE_CORE:
            exact = (
                bool(quote)
                and normalize_for_match(quote) == normalize_for_match(company_line.text)
                and len(term_tokens(company_line.text)) >= 2
            )
            match = MATCH_EXACT_CORE if exact else MATCH_SEMANTIC_CORE
        elif role == ROLE_ADJACENT:
            match = MATCH_ADJACENT
        elif role == ROLE_SUPPORT:
            match = MATCH_SUPPORT
        elif role == ROLE_GENERIC:
            match = MATCH_GENERIC
        else:
            match = MATCH_NONE
        return match, MATCH_RANK[match]
