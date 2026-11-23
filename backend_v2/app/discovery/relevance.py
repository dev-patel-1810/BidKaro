"""Deterministic, explainable profile fit for discovered tenders.

Fit is an ordered evidence ladder, not a score-first sum:

1. an exact multi-word **core** work phrase from the company brief;
2. another **core** work match (a one-word core term or a partial token);
3. an **adjacent** declared work match, with the company's region;
4. an adjacent declared work match alone;
5. a **support** term (``technical support``, ``document retrieval``) with the
   region;
6. a support term alone, or a **generic** recall word alone;
7. nothing, or the AI could not tell.

The distinction matters. A confirmed capability is not automatically core:
``technical support`` is support, while ``document scanning`` is core. Generic
words such as ``technology`` or ``web pages`` may find a notice, but they never
rank it above the lowest visible rung. The editable company brief supplies the
roles; before a brief is generated the same deterministic role rules are
applied to the profile and confirmed understanding.

The matcher works on the *work phrase*, never on the buyer. Places, department
names, organisation acronyms and boilerplate words (``computer``, ``document``,
``software``, ``entry``, ``high``, ``technical``) are removed first by
:mod:`app.discovery.work_text`. That is what stops a scanning firm matching a
public works department because the department's name contains "technical".

This module never decides eligibility. A high fit cannot pass a mandatory
criterion, and the assessment remains the only place that does.
"""

from __future__ import annotations

from typing import Any

from app.discovery.models import Relevance
from app.discovery.roles import (
    ROLE_ADJACENT,
    ROLE_CORE,
    ROLE_GENERIC,
    ROLE_SUPPORT,
    classify_term,
    term_text,
)
from app.discovery.work_text import (
    ACRONYM_EXPANSIONS,
    CONNECTORS,
    clean_term,
    contains,
    normalize_text,
    term_match,
    term_tokens,
    term_variants as _term_variants,
    tokens,
    work_text,
)

MIN_TERM_LENGTH = 3

#: Bumped whenever the matching rules change. A stored verdict stamped with an
#: older version is recomputed rather than reused.
RULES_VERSION = "2026-11-22.1"

#: How common a term is in the corpus. Used for an explanation only; the tier
#: ladder is the primary rule and a rare word never promotes a tier on its own.
RARE_SHARE = 0.02

#: How much one human "not relevant" for a work type demotes a match. Small on
#: purpose: it reorders and demotes, it never bans a word.
DEMOTE_PENALTY = 0.15

BUCKETS = ("strong", "possible", "hidden")

#: The score each tier may show. The ranges never touch 1.0: a tender match is
#: not a guarantee, so the UI is capped at 95%.
TIER_BANDS: dict[int, tuple[float, float]] = {
    1: (0.90, 0.95),
    2: (0.80, 0.88),
    3: (0.70, 0.78),
    4: (0.60, 0.68),
    5: (0.50, 0.58),
    6: (0.40, 0.48),
    7: (0.00, 0.25),
}
GLOBAL_SCORE_CAP = 0.95

#: An all-India "region" is not a place that can break a tie.
GENERIC_REGIONS = frozenset({"india", "indian", "national", "all india"})


def term_variants(term: str) -> list[str]:
    """Public alias kept for callers that only need the prefilter variants."""

    return _term_variants(term)


def band_score(tier: int, evidence: int = 0) -> float:
    """A deterministic score inside the tier's band, never above 95%."""

    low, high = TIER_BANDS.get(int(tier), TIER_BANDS[7])
    if int(tier) >= 7:
        return 0.0
    bump = min(high - low, 0.01 * max(0, int(evidence)))
    return round(min(GLOBAL_SCORE_CAP, low + bump), 4)


def tier_for_score(score: float | None) -> int:
    """The ladder rung a stored score belongs to.

    A score in a gap between two bands (for example 0.77) goes to the nearest
    band, so a hand-edited or old row still has an honest tier.
    """

    value = max(0.0, min(1.0, float(score or 0.0)))
    best_tier = 7
    best_distance = float("inf")
    for tier, (low, high) in TIER_BANDS.items():
        if low <= value <= high:
            return tier
        distance = min(abs(value - low), abs(value - high))
        if distance < best_distance:
            best_distance = distance
            best_tier = tier
    return best_tier


def _dedupe_plain(terms: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in terms or []:
        text = normalize_text(value)
        if len(text) >= 2 and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _clean_terms(
    terms: list[str] | None, *, allow_short_acronym: bool = False
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in terms or []:
        cleaned = clean_term(value, allow_short_acronym=allow_short_acronym)
        if not cleaned or len(cleaned) < MIN_TERM_LENGTH:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _brief_groups(company: dict) -> dict[str, list[str]] | None:
    """Read the four role groups from a brief, when one is supplied."""

    brief = company.get("brief")
    if not isinstance(brief, dict):
        return None
    keys = {
        ROLE_CORE: ("core_terms", "core"),
        ROLE_ADJACENT: ("adjacent_terms", "adjacent"),
        ROLE_SUPPORT: ("support_terms", "support"),
        ROLE_GENERIC: ("generic_terms", "generic"),
    }
    out: dict[str, list[str]] = {}
    found = False
    for role, names in keys.items():
        values: list[Any] = []
        for name in names:
            candidate = brief.get(name)
            if candidate:
                values = candidate if isinstance(candidate, list) else [candidate]
                found = True
                break
        cleaned = _clean_terms(
            [term_text(item) for item in values], allow_short_acronym=True
        )
        out[role] = cleaned
    if not found:
        return None
    # A term belongs to the strongest group only, so one phrase never counts
    # twice because it was copied between sections during an edit.
    seen: set[str] = set()
    for role in (ROLE_CORE, ROLE_ADJACENT, ROLE_SUPPORT, ROLE_GENERIC):
        retained: list[str] = []
        for term in out[role]:
            if term in seen:
                continue
            seen.add(term)
            retained.append(term)
        out[role] = retained
    return out


def _role_split(
    *,
    understanding: list[str],
    capability: list[str],
    keywords: list[str],
) -> dict[str, list[str]]:
    """Apply the role rules to a profile when no stored brief exists."""

    out: dict[str, list[str]] = {
        ROLE_CORE: [],
        ROLE_ADJACENT: [],
        ROLE_SUPPORT: [],
        ROLE_GENERIC: [],
    }
    seen: set[str] = set()

    def add(term: str, role: str) -> None:
        if not term or term in seen:
            return
        seen.add(term)
        out[role].append(term)

    for term in understanding:
        add(term, classify_term(term, confirmed=True))
    for term in capability + keywords:
        role = classify_term(term, confirmed=False)
        # A declared profile term is adjacent, never core: only a confirmed
        # capability or a delivered project is core evidence.
        if role == ROLE_CORE:
            role = ROLE_ADJACENT
        add(term, role)
    return out


def company_terms(company: dict) -> dict[str, list[str]]:
    """Split a profile into ordered, cleaned role groups.

    A stored ``brief`` wins. Otherwise the same deterministic role rules are
    applied to confirmed understanding (core/support/generic) and to the
    declared services/sectors/keywords (adjacent/support/generic). A phrase is
    removed from a weaker group when a stronger group already states it.
    """

    preferences = company.get("preferences") or {}
    understanding = _clean_terms(
        _as_list(company.get("understanding"))
        + _as_list(preferences.get("understanding")),
        allow_short_acronym=True,
    )
    capability = _clean_terms(
        _as_list(company.get("products_services"))
        + _as_list(company.get("services"))
        + _as_list(company.get("sectors"))
        + _as_list(preferences.get("services"))
        + _as_list(preferences.get("sectors"))
    )
    keywords = _clean_terms(
        _as_list(company.get("keywords"))
        + _as_list(preferences.get("keywords"))
    )
    brief = _brief_groups(company)
    if brief is not None:
        groups = brief
    else:
        groups = _role_split(
            understanding=understanding,
            capability=capability,
            keywords=keywords,
        )
    regions = _dedupe_plain(
        _as_list(company.get("regions"))
        + _as_list(company.get("region"))
        + _as_list(preferences.get("regions"))
    )
    return {
        ROLE_CORE: groups[ROLE_CORE],
        ROLE_ADJACENT: groups[ROLE_ADJACENT],
        ROLE_SUPPORT: groups[ROLE_SUPPORT],
        ROLE_GENERIC: groups[ROLE_GENERIC],
        # Compatibility aliases for older callers. The ladder reads the role
        # keys above; these keep a third-party import from breaking.
        "understanding": groups[ROLE_CORE],
        "capability": groups[ROLE_ADJACENT],
        "keywords": groups[ROLE_SUPPORT] + groups[ROLE_GENERIC],
        "regions": regions,
        "document": [],
    }


def has_any_terms(company: dict) -> bool:
    terms = company_terms(company)
    return bool(
        terms[ROLE_CORE]
        or terms[ROLE_ADJACENT]
        or terms[ROLE_SUPPORT]
        or terms[ROLE_GENERIC]
    )


def profile_signature(
    company: Any,
    *,
    understanding_terms: list[str] | None = None,
    brief_terms: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """The profile inputs a cached verdict depends on.

    The read path and the background pass must call this one helper, otherwise
    two hashes drift and every read is a cache miss. The brief's role groups are
    included, so editing a term from support to core (or the reverse) recomputes
    every verdict.
    """

    if isinstance(company, dict):
        preferences = company.get("preferences") or {}
        base: dict[str, Any] = {
            "services": _as_list(company.get("products_services"))
            or _as_list(company.get("services")),
            "sectors": _as_list(company.get("sectors")),
            "keywords": _as_list(preferences.get("keywords"))
            or _as_list(company.get("keywords")),
            "region": _as_list(company.get("regions"))
            or _as_list(company.get("region")),
            "profile": company.get("profile"),
        }
    else:
        base = {
            "services": _as_list(getattr(company, "services", None)),
            "sectors": _as_list(getattr(company, "sectors", None)),
            "keywords": _as_list(getattr(company, "keywords", None)),
            "region": getattr(company, "region", None),
            "profile": getattr(company, "profile", None),
        }
    base["understanding_terms"] = sorted(
        _dedupe_plain(_as_list(understanding_terms))
    )
    if brief_terms is not None:
        base["brief"] = {
            role: sorted(_dedupe_plain(_as_list(brief_terms.get(role))))
            for role in (ROLE_CORE, ROLE_ADJACENT, ROLE_SUPPORT, ROLE_GENERIC)
        }
    return _stable_value(base)


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple, set)):
        return sorted((_stable_value(item) for item in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def prefilter_terms(terms: list[str]) -> list[str]:
    """A SQL contains-prefilter that is a superset of the work matcher.

    It includes the phrase variants, every token of a phrase, and both sides of
    a known acronym expansion. A prefilter that misses a row the gate would keep
    hides it from the list; a prefilter that is too broad only reads a few more
    rows, which the gate then judges.
    """

    out: list[str] = []
    for term in terms:
        out.extend(term_variants(term))
        for token in term_tokens(term):
            if len(token) >= MIN_TERM_LENGTH:
                out.append(token)
        base = normalize_text(term)
        if base in ACRONYM_EXPANSIONS:
            out.extend(term_tokens(ACRONYM_EXPANSIONS[base]))
        for acronym, expansion in ACRONYM_EXPANSIONS.items():
            if base == expansion:
                out.append(acronym)
    return list(dict.fromkeys(value for value in out if value))


def understanding_terms_from_rows(
    capabilities: list[Any] | None,
    experiences: list[Any] | None,
) -> list[str]:
    """The confirmed phrases that may feed fit.

    A capability contributes its name and its confirmed aliases. An experience
    contributes the work family a human confirmed; its project title is a proper
    noun, not a kind of work, so it is never used. A name or work family is
    cleaned as ordinary work (so an organisation acronym such as ``MAHSR`` is
    not work evidence), while an alias may be a short acronym the human wrote
    deliberately (``DMS``).
    """

    names: list[str] = []
    aliases: list[str] = []
    for capability in capabilities or []:
        names.append(str(getattr(capability, "name", "") or ""))
        values = getattr(capability, "aliases", None) or []
        if isinstance(values, str):
            values = [values]
        aliases.extend(str(alias) for alias in values)
    for experience in experiences or []:
        names.append(str(getattr(experience, "work_family", "") or ""))
    cleaned = _clean_terms(names, allow_short_acronym=False)
    for alias in _clean_terms(aliases, allow_short_acronym=True):
        if alias not in cleaned:
            cleaned.append(alias)
    return cleaned


def _match_group(work: str, terms: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    full: list[str] = []
    partial: list[tuple[str, str]] = []
    for term in terms:
        result = term_match(work, term)
        if not result:
            continue
        kind, token = result
        if kind == "full":
            full.append(term)
        elif token:
            partial.append((term, token))
    return full, partial


def _support_units(full: list[str], partial: list[tuple[str, str]]) -> tuple[int, int]:
    full_units = sum(2 if len(term_tokens(term)) >= 2 else 1 for term in full)
    return full_units, len(partial)


def _region_match(region_words: str, regions: list[str]) -> bool:
    if not region_words or not regions:
        return False
    text = normalize_text(region_words)
    for region in regions:
        base = normalize_text(region)
        if not base or base in GENERIC_REGIONS:
            continue
        if contains(text, base):
            return True
        place_tokens = [
            token
            for token in tokens(base)
            if token not in CONNECTORS and token not in GENERIC_REGIONS
        ]
        if place_tokens and any(contains(text, token) for token in place_tokens):
            return True
    return False


def _rejected_terms(
    matched: list[str],
    partial: list[tuple[str, str]],
    negative: dict[str, int] | None,
) -> list[str]:
    if not negative:
        return []
    keys = list(negative)
    rejected: list[str] = []
    for term in matched:
        if term in negative:
            rejected.append(term)
            continue
        # A stored rejection is often a phrase ("road resurfacing") or one
        # word ("road") of the same work. Either should demote, not just an
        # exact repeat of the current wording.
        term_parts = set(term_tokens(term))
        if any(
            key in term_parts
            or any(part in term_parts for part in term_tokens(key))
            for key in keys
        ):
            rejected.append(term)
    for term, token in partial:
        if term in negative:
            rejected.append(term)
            continue
        if any(token == key or token in term_tokens(key) for key in keys):
            rejected.append(term)
    return list(dict.fromkeys(rejected))


def _assess_terms(
    terms: dict[str, list[str]],
    *,
    work: str,
    region_words: str,
    rarity: dict[str, float] | None = None,
    negative: dict[str, int] | None = None,
) -> dict[str, Any]:
    work = normalize_text(work)
    core_full, core_partial = _match_group(work, terms.get(ROLE_CORE, []))
    adjacent_full, adjacent_partial = _match_group(
        work, terms.get(ROLE_ADJACENT, [])
    )
    support_full, support_partial = _match_group(
        work, terms.get(ROLE_SUPPORT, [])
    )
    generic_full, generic_partial = _match_group(
        work, terms.get(ROLE_GENERIC, [])
    )

    core_units = _support_units(core_full, core_partial)
    adjacent_units = _support_units(adjacent_full, adjacent_partial)
    support_units = _support_units(support_full, support_partial)
    generic_units = _support_units(generic_full, generic_partial)
    region_match = _region_match(region_words, terms.get("regions", []))

    # The evidence ladder. An exact multi-word core phrase is the strongest
    # signal; a confirmed single word or a partial token is the next rung. A
    # support-only phrase can never sit above an adjacent work match, and a
    # generic word can never lift itself above the bottom visible rung.
    if core_full and any(len(term_tokens(term)) >= 2 for term in core_full):
        tier = 1
    elif core_full or core_partial:
        tier = 2
    elif adjacent_full or adjacent_partial:
        tier = 3 if region_match else 4
    elif support_full or support_partial:
        tier = 5 if region_match else 6
    elif generic_full or generic_partial:
        tier = 6
    else:
        tier = 7

    matched_terms: list[str] = []
    reasons: list[str] = []

    for term in core_full:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(f"Matches your core work: {term}")
    for term, token in core_partial:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(
            f"Related to your core work: {term} (the notice says '{token}')"
        )
    for term in adjacent_full:
        if term not in matched_terms:
            matched_terms.append(term)
        share = (rarity or {}).get(term, 0.0)
        if share >= 0.2:
            reasons.append(
                f"Matches your adjacent declared work: {term} "
                "(common in this corpus, so it ranks low)"
            )
        else:
            reasons.append(f"Matches your adjacent declared work: {term}")
    for term, token in adjacent_partial:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(
            f"Related to your adjacent declared work: {term} "
            f"(the notice says '{token}')"
        )
    for term in support_full:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(
            f"Matches a support term: {term} (delivery, not core work)"
        )
    for term, token in support_partial:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(
            f"Related to a support term: {term} (the notice says '{token}')"
        )
    for term in generic_full:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(
            f"Generic recall term: {term} (used to find the notice, not to rank it)"
        )
    for term, token in generic_partial:
        if term not in matched_terms:
            matched_terms.append(term)
        reasons.append(
            f"Generic recall term: {term} (the notice says '{token}'; "
            "it does not rank the notice)"
        )

    if tier == 7:
        return {
            "bucket": "hidden",
            "tier": 7,
            "score": 0.0,
            "matched_terms": [],
            "reasons": [
                "The work does not match your core, adjacent or support work profile."
            ],
            "profile_complete": True,
            "region_match": region_match,
        }

    if region_match and tier in (3, 5):
        reasons.append("The notice is in your region, which breaks the tie.")
    elif region_match:
        reasons.append("The notice is also in your region.")

    score = band_score(
        tier,
        max(1, core_units + adjacent_units + support_units + generic_units),
    )
    rejected = _rejected_terms(
        matched_terms,
        core_partial
        + adjacent_partial
        + support_partial
        + generic_partial,
        negative,
    )
    if rejected:
        # A human said this kind of work is not theirs. Demote, never ban: the
        # row stays visible, at the bottom of the possible band.
        score = max(0.0, score - DEMOTE_PENALTY * len(rejected))
        tier = min(6, max(4, tier_for_score(score)))
        score = band_score(tier, max(1, support_units))
        reasons.append(
            "You marked similar work as not relevant: "
            + ", ".join(sorted(rejected))
            + ". It is demoted, not hidden."
        )

    bucket = "strong" if tier <= 3 else "possible"
    return {
        "bucket": bucket,
        "tier": tier,
        "score": score,
        "matched_terms": list(dict.fromkeys(matched_terms)),
        "reasons": list(dict.fromkeys(reasons)),
        "profile_complete": True,
        "region_match": region_match,
    }


def score_tender(
    company: dict,
    *,
    title: str,
    snippet: str = "",
    text: str = "",
    location: str | None = None,
    context: str = "",
    threshold: float = 0.3,
    brief_terms: dict[str, list[str]] | None = None,
) -> Relevance:
    """Score one notice against a plain company profile.

    Only ``title`` and ``snippet`` decide the work. ``text`` is a fallback when
    the caller has no title or snippet; reading a whole notice body would let a
    requirement ("experience in road construction") become work evidence.
    ``context`` and ``location`` are used for the region tie-break only.
    """

    work = work_text(title, snippet)
    if not work and text:
        work = work_text(text[:2_000])
    region_words = normalize_text(
        " ".join(part for part in (title or "", location or "", context or "") if part)
    )
    profile = dict(company)
    if brief_terms is not None:
        profile["brief"] = brief_terms
    terms = company_terms(profile)
    if not (
        terms[ROLE_CORE]
        or terms[ROLE_ADJACENT]
        or terms[ROLE_SUPPORT]
        or terms[ROLE_GENERIC]
    ):
        return Relevance(
            score=0.0,
            tier=7,
            matched_terms=[],
            reasons=["No work profile declared yet."],
            threshold=threshold,
        )
    result = _assess_terms(
        terms,
        work=work,
        region_words=region_words,
    )
    return Relevance(
        score=result["score"],
        tier=result["tier"],
        matched_terms=result["matched_terms"],
        reasons=result["reasons"],
        threshold=threshold,
        work_type_match=result["bucket"] != "hidden",
        region_match=result["region_match"],
    )


def work_type_match(
    company: dict,
    *,
    title: str,
    snippet: str = "",
    text: str = "",
    location: str | None = None,
    context: str = "",
    threshold: float = 0.3,
    brief_terms: dict[str, list[str]] | None = None,
) -> bool:
    """The listing gate's own rule, exposed for a portal walk's row filter."""

    return score_tender(
        company,
        title=title,
        snippet=snippet,
        text=text,
        location=location,
        context=context,
        threshold=threshold,
        brief_terms=brief_terms,
    ).work_type_match


def company_profile_dict(
    company: Any,
    *,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
    brief_terms: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Adapt a v2 ``Company`` row to the walk's plain dictionary.

    ``document_terms`` is accepted for compatibility and deliberately ignored:
    raw about-company terms do not feed fit. ``understanding_terms`` are the
    human-confirmed capability/experience phrases; ``brief_terms`` are the four
    role groups from the editable brief.
    """

    profile = getattr(company, "profile", None) or {}
    if not isinstance(profile, dict):
        profile = {}

    def as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    keywords = as_list(getattr(company, "keywords", None)) + as_list(
        profile.get("keywords")
    )
    capability = (
        as_list(getattr(company, "services", None))
        + as_list(getattr(company, "sectors", None))
        + as_list(profile.get("services"))
        + as_list(profile.get("sectors"))
        + as_list(profile.get("products_services"))
        + as_list(profile.get("product_services"))
    )
    regions = as_list(getattr(company, "region", None)) + as_list(
        profile.get("regions")
    )
    understanding = (
        as_list(understanding_terms)
        + as_list(getattr(company, "understanding", None))
        + as_list(profile.get("understanding"))
    )
    return {
        "products_services": capability,
        "sectors": as_list(getattr(company, "sectors", None))
        + as_list(profile.get("sectors")),
        "preferences": {
            "keywords": keywords,
            "sectors": as_list(getattr(company, "sectors", None))
            + as_list(profile.get("sectors")),
            "regions": regions,
            "understanding": understanding,
            "document_terms": [],
        },
        "region": as_list(getattr(company, "region", None)),
        "regions": regions,
        "understanding": understanding,
        "brief": brief_terms,
    }


def profile_is_complete(
    company: Any,
    *,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
    brief_terms: dict[str, list[str]] | None = None,
) -> bool:
    """True when the profile says what work the company does, not only where."""

    if isinstance(company, dict):
        profile = dict(company)
        if brief_terms is not None:
            profile["brief"] = brief_terms
        terms = company_terms(profile)
    else:
        terms = company_terms(
            company_profile_dict(
                company,
                understanding_terms=understanding_terms,
                brief_terms=brief_terms,
            )
        )
    return bool(
        terms[ROLE_CORE]
        or terms[ROLE_ADJACENT]
        or terms[ROLE_SUPPORT]
        or terms[ROLE_GENERIC]
    )


def rarity_from_titles(titles: list[str], terms: list[str]) -> dict[str, float]:
    """How common each term is in the candidate titles, for explanation only."""

    cleaned_titles = [work_text(title) for title in titles if title]
    cleaned_titles = [title for title in cleaned_titles if title]
    total = len(cleaned_titles)
    if total == 0 or not terms:
        return {}
    shares: dict[str, float] = {}
    for term in terms:
        hits = sum(1 for title in cleaned_titles if term_match(title, term))
        shares[term] = hits / total
    return shares


def _tender_work(tender: Any) -> str:
    return work_text(
        getattr(tender, "work_title", None),
        getattr(tender, "work_type", None),
    )


def _tender_region_words(tender: Any) -> str:
    return normalize_text(
        " ".join(
            str(part)
            for part in (
                getattr(tender, "work_title", None),
                getattr(tender, "location", None),
                getattr(tender, "region", None),
                getattr(tender, "organisation_chain", None),
            )
            if part
        )
    )


def classify(
    company: Any,
    tender: Any,
    *,
    rarity: dict[str, float] | None = None,
    negative: dict[str, int] | None = None,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
    brief_terms: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """The three-way listing verdict plus its ladder tier.

    ``strong`` covers tiers 1-3, ``possible`` covers 4-6 and ``hidden`` is tier
    7. A profile with no work at all is ``possible`` with tier 7 so the listing
    can show the whole corpus under an "all tenders" heading; it is not a
    match and the route says the profile is incomplete.
    """

    if isinstance(company, dict):
        profile = dict(company)
        if brief_terms is not None:
            profile["brief"] = brief_terms
    else:
        profile = company_profile_dict(
            company,
            understanding_terms=understanding_terms,
            brief_terms=brief_terms,
        )
    terms = company_terms(profile)
    if not (
        terms[ROLE_CORE]
        or terms[ROLE_ADJACENT]
        or terms[ROLE_SUPPORT]
        or terms[ROLE_GENERIC]
    ):
        return {
            "bucket": "possible",
            "tier": 7,
            "score": 0.0,
            "matched_terms": [],
            "reasons": ["No work profile declared yet."],
            "profile_complete": False,
            "region_match": False,
        }

    result = _assess_terms(
        terms,
        work=_tender_work(tender),
        region_words=_tender_region_words(tender),
        rarity=rarity,
        negative=negative,
    )
    result["profile_complete"] = True
    return result


def tender_matches_company(
    company: Any,
    tender: Any,
    *,
    rarity: dict[str, float] | None = None,
    negative: dict[str, int] | None = None,
    document_terms: list[str] | None = None,
    understanding_terms: list[str] | None = None,
    brief_terms: dict[str, list[str]] | None = None,
) -> bool:
    """The read-time listing gate: strong and possible are visible."""

    if getattr(tender, "source_type", None) == "manual_upload":
        return True
    return (
        classify(
            company,
            tender,
            rarity=rarity,
            negative=negative,
            document_terms=document_terms,
            understanding_terms=understanding_terms,
            brief_terms=brief_terms,
        )["bucket"]
        != "hidden"
    )
