"""Deterministic corpus rules: identity, citation text and small parsers.

These functions know nothing about the database. The repository stores what
they return; the service layer decides when to call them.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, time, timezone
from pathlib import Path

MIN_EXCERPT_CHARS = 20
MAX_EXCERPT_CHARS = 5000
MAX_TITLE_CHARS = 200
MAX_OBLIGATION_CHARS = 2000
MAX_REFERENCE_CHARS = 160

_QUOTE_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DOT_LEADER = re.compile(r"\.{4,}")


def trim(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned[:limit] or None


def normalize_for_match(text: str) -> str:
    """Whitespace/typography-insensitive form used only for verification."""
    normalized = unicodedata.normalize("NFKC", text or "").translate(_QUOTE_MAP)
    return " ".join(normalized.split()).lower()


def excerpt_hash(excerpt: str) -> str:
    return hashlib.sha256(normalize_for_match(excerpt).encode("utf-8")).hexdigest()


def _identity_part(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return _NON_ALNUM.sub("", normalized)


def tender_dedupe_key(
    *,
    portal_key: str | None,
    reference: str | None,
    work_title: str | None,
    organisation: str | None,
) -> str:
    """Portal identity or reference/title/organisation, never a date."""
    parts = [
        _identity_part(portal_key),
        _identity_part(reference),
        _identity_part(work_title),
        _identity_part(organisation),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def manual_dedupe_key(*, work_title: str | None, first_document_sha: str) -> str:
    parts = [
        "manual",
        _identity_part(work_title),
        _identity_part(first_document_sha),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def document_key(filename: str | None) -> str:
    stem = Path(filename or "document").stem
    normalized = unicodedata.normalize("NFKC", stem).casefold()
    key = _NON_ALNUM.sub("-", normalized).strip("-")
    return (key or "document")[:120]


def parse_deadline_at(value: str | None) -> datetime | None:
    """Parse an ISO date or datetime. Date-only values are anchored at noon UTC.

    A calendar day must survive display in IST; midnight UTC would show the
    previous day for an early-morning reading in India.
    """

    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def map_category(value: str | None) -> str:
    cleaned = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in {"eligibility", "technical", "financial", "qualification"}:
        return "eligibility"
    if cleaned in {"submission", "bid_submission"}:
        return "submission"
    if cleaned in {"form", "forms", "document"}:
        return "form"
    if cleaned in {"performance", "contract", "post_award"}:
        return "performance"
    if cleaned in {"compliance", "legal", "statutory"}:
        return "compliance"
    return "compliance"


def map_mandatory(value: str | None) -> str:
    cleaned = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in {"yes", "true", "mandatory", "required", "shall"}:
        return "mandatory"
    if cleaned in {"no", "false", "optional", "not_mandatory", "notrequired"}:
        return "not_mandatory"
    return "unknown"


def map_phase(value: str | None) -> str:
    return "post_award" if (value or "").strip().lower() == "post_award" else "pre_award"


def is_navigation_excerpt(excerpt: str, extra_text: str | None = None) -> bool:
    """True for a contents/index hit, never for a normal obligation.

    A contents row is short, uses dot leaders, and ends with a page number. A
    real clause can mention a page or a number without looking like a list of
    section names, so the test requires the shape of a contents entry rather
    than the presence of digits.
    """

    text = normalize_for_match(f"{extra_text or ''} {excerpt or ''}").strip()
    if not text:
        return False
    if re.match(r"^(table\s+of\s+contents|contents|index)\b", text):
        return True

    lines = [line.strip() for line in (excerpt or "").splitlines() if line.strip()]
    if len(lines) >= 3:
        ending_in_number = sum(1 for line in lines if re.search(r"\d+\s*$", line))
        if ending_in_number / len(lines) >= 0.6:
            return True

    if _DOT_LEADER.search(excerpt or ""):
        # A contents row such as "Section 3 ..... 12". A long prose paragraph
        # with an accidental ellipsis is not navigation.
        if len(excerpt) <= 300 and re.search(r"\d+\s*$", excerpt.strip()):
            return True
    return False


def next_version(current_version: str | None) -> str:
    if not current_version:
        return "v1"
    match = re.fullmatch(r"v(\d+)", current_version)
    if not match:
        return "v1"
    return f"v{int(match.group(1)) + 1}"
