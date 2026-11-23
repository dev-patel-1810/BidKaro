"""What kind of work a tender is, computed once for the whole corpus.

A tender's work type does not depend on which company is reading it, so it is
computed once and reused. The label is derived from the work title itself: the
significant words of the title, in order, with grammatical function words
removed and the reference and tender id taken out. There is no trade
vocabulary in this file. "Construction", "supply", "repair" and every other
work word come from the tender, which is the only way a corpus of government
tenders stays honest as new kinds of work appear.

The result is stamped with a rules version and the tender's content hash, so
either a changed tender or an improved ruler invalidates it.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

#: Bumped when the extraction below changes. A classification stamped with an
#: older version is recomputed rather than reused.
CLASSIFIER_VERSION = "2026-11-16.1"

#: Words that carry no work meaning. This is grammar, not a trade vocabulary:
#: articles, prepositions, conjunctions and the verbs of tender boilerplate.
#: Nothing here names a kind of work, so no authority or industry is baked in.
FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "under",
        "with",
        "without",
        "via",
        "per",
        "including",
        "etc",
        "other",
        "others",
        "various",
        "misc",
        "miscellaneous",
        "work",
        "works",
        "tender",
        "tenders",
        "notice",
        "bid",
        "bids",
        "bidder",
        "bidders",
        "please",
        "kindly",
        "may",
        "shall",
        "will",
    }
)

_WORD = re.compile(r"[a-z][a-z0-9+#./-]*", re.IGNORECASE)
_BRACKET_GROUP = re.compile(r"\[[^\]]*\]")
#: A portal tender id (``2026_NHAI_295179_1``) and a reference (``NHAI/2026/1``)
#: are identifiers, not work words. They are removed before labelling.
_PORTAL_ID = re.compile(r"\b\d{4}_[A-Z]{2,10}_\d{3,}_\d+\b", re.IGNORECASE)
_REFERENCE = re.compile(r"\b[A-Z]{2,10}[/-]\d{2,4}[/-][A-Z0-9/-]+\b")

MAX_WORK_TYPES = 6


def _normalise(title: str | None) -> str:
    text = str(title or "")
    text = _BRACKET_GROUP.sub(" ", text)
    text = _PORTAL_ID.sub(" ", text)
    text = _REFERENCE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def significant_words(title: str | None) -> list[str]:
    """The title's work words, in the order they were written."""

    words: list[str] = []
    for match in _WORD.finditer(_normalise(title)):
        word = match.group(0).strip(".-/")
        if len(word) < 3:
            continue
        if word in FUNCTION_WORDS:
            continue
        if word.isdigit():
            continue
        if word not in words:
            words.append(word)
    return words


def classify_title(title: str | None) -> dict[str, Any]:
    """The label, its confidence and a readable reason.

    Confidence is deliberately conservative. A short title gives few words,
    so the label is weakest exactly where a reader would also be least sure.
    """

    words = significant_words(title)
    label = words[0] if words else None
    work_types = words[:MAX_WORK_TYPES]
    if not words:
        confidence = 0.0
    elif len(words) == 1:
        confidence = 0.3
    elif len(words) == 2:
        confidence = 0.5
    else:
        confidence = 0.7
    return {
        "primary_work_type": label,
        "work_types": work_types,
        "confidence": confidence,
        "reason": (
            "No work words could be read from the title."
            if not words
            else "Work words read from the title: " + ", ".join(work_types) + "."
        ),
    }


def content_hash(tender: Any) -> str:
    """A hash of everything the classifier reads, so a change invalidates it."""

    payload = {
        "title": getattr(tender, "work_title", None),
        "type": getattr(tender, "work_type", None),
        "location": getattr(tender, "location", None),
        "reference": getattr(tender, "reference", None),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
