"""Deterministic profile terms for the soft suitability score.

This is the assessment's 25% profile-fit dimension, not the Discover ladder.
It counts how many of the company's declared work terms the tender's work text
names. Raw about-company terms are not included: only a human-confirmed
capability/experience or a declared sector/service/keyword may match.

The tender text is reduced by :mod:`app.discovery.work_text` first, so a place,
a department name or an organisation acronym is never work evidence.
"""

from __future__ import annotations

from typing import Any

from app.discovery.work_text import (
    clean_term,
    term_match,
    term_variants as _term_variants,
    work_text,
)

MIN_TERM_LENGTH = 3
PROFILE_FIT_WEIGHT = 25


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _dedupe_terms(terms: list[str] | None) -> list[str]:
    seen: dict[str, str] = {}
    for term in terms or []:
        cleaned = clean_term(term)
        if cleaned and len(cleaned) >= MIN_TERM_LENGTH and cleaned not in seen:
            seen[cleaned] = cleaned
    return list(seen.values())


def company_profile_terms(
    company: Any,
    *,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
    brief_terms: dict[str, list[str]] | None = None,
) -> list[str]:
    """Sectors, services, keywords and confirmed capabilities.

    The region is deliberately not a term here. It decides ranking in
    discovery; the fit score is about whether the work is one the company does.
    ``document_terms`` is accepted for compatibility and ignored: raw harvested
    words are not work evidence until a human confirms a capability from them.
    When a brief is supplied, its core, adjacent and support terms are the
    profile; generic recall terms are deliberately excluded from the soft score.
    """

    if brief_terms:
        raw: list[str] = []
        for role in ("core", "adjacent", "support"):
            raw.extend(_as_list(brief_terms.get(role)))
        return _dedupe_terms(raw)

    raw: list[str] = []
    for attribute in ("sectors", "services", "keywords"):
        raw.extend(_as_list(getattr(company, attribute, None)))
    profile = getattr(company, "profile", None) or {}
    if isinstance(profile, dict):
        for key in (
            "sectors",
            "services",
            "products_services",
            "product_services",
            "keywords",
            "understanding",
        ):
            raw.extend(_as_list(profile.get(key)))
    raw.extend(_as_list(understanding_terms))
    return _dedupe_terms(raw)


def term_variants(term: str) -> list[str]:
    """Kept as a public wrapper; the one implementation lives in work_text."""

    return _term_variants(term)


def tender_profile_text(tender: Any) -> str:
    """The words that describe the work, never the body or the place.

    The old version kept ``location`` and ``region``. A place is where the work
    is, not what it is, so it is not part of the work text any more.
    """

    return work_text(
        getattr(tender, "work_title", None),
        getattr(tender, "work_type", None),
    )


def matched_terms(
    company: Any,
    tender: Any,
    *,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
) -> tuple[list[str], list[str], int]:
    """Return (matched terms, readable matches, total profile terms)."""

    terms = company_profile_terms(
        company, understanding_terms=understanding_terms
    )
    if not terms:
        return [], [], 0
    haystack = tender_profile_text(tender)
    matched: list[str] = []
    reasons: list[str] = []
    for term in terms:
        result = term_match(haystack, term)
        if not result:
            continue
        kind, token = result
        matched.append(term)
        if kind == "partial" and token:
            reasons.append(
                f"Related to profile term: {term} (the notice says '{token}')"
            )
        else:
            reasons.append(f"Matches profile term: {term}")
    return matched, reasons, len(terms)


def discovery_relevance(
    company: Any,
    tender: Any,
    *,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
) -> dict[str, Any]:
    """The same ladder the Discover list uses, for callers that need a dict."""

    from app.discovery.relevance import classify

    verdict = classify(
        company,
        tender,
        document_terms=document_terms,
        understanding_terms=understanding_terms,
    )
    return {
        "score": verdict["score"],
        "tier": verdict["tier"],
        "bucket": verdict["bucket"],
        "matched_terms": verdict["matched_terms"],
        "reasons": verdict["reasons"],
        "match_reasons": verdict["reasons"],
        "region_match": verdict["region_match"],
        "profile_complete": verdict["profile_complete"],
    }


def profile_fit(company: Any, tender: Any) -> tuple[float, list[str], str]:
    matched, reasons, total = matched_terms(company, tender)
    if total == 0:
        return 0.0, [], "No declared sector, service or keyword."
    points = round(PROFILE_FIT_WEIGHT * len(matched) / total, 2)
    if matched:
        reason = (
            f"{len(matched)} of {total} profile terms matched: "
            + ", ".join(matched[:8])
            + ("." if len(matched) <= 8 else ", ...")
        )
    else:
        reason = "No declared sector, service or keyword matched this tender."
    return points, reasons, reason
