"""What role a company term plays in fit ranking.

A company profile mixes four very different kinds of words:

* **core** work: the specific kind of work the company does (``document
  scanning``, ``examination processing``);
* **adjacent** work: a declared service or sector that is specific, but has not
  been confirmed as a capability from the company's own document;
* **support** words: a real service that says *how* the work is delivered
  (``technical support``, ``document retrieval``, ``version control``), not the
  work itself;
* **generic** words: a bare technology or department word (``technology``,
  ``software``, ``web pages``) that is useful for recall and useless for
  ranking.

The distinction is deliberately small and deterministic. It is not a work
vocabulary: it names only the words that describe delivery, not a list of
approved industries. A word that is not support/generic stays specific, so a
company can add a new kind of work without editing code.
"""

from __future__ import annotations

from app.discovery.work_text import (
    CONNECTORS,
    GENERIC_WORK_WORDS,
    NOISE_WORDS,
    clean_term,
    normalize_text,
    term_tokens,
)

ROLE_CORE = "core"
ROLE_ADJACENT = "adjacent"
ROLE_SUPPORT = "support"
ROLE_GENERIC = "generic"
ROLES = (ROLE_CORE, ROLE_ADJACENT, ROLE_SUPPORT, ROLE_GENERIC)

#: Words that describe how a service is delivered. They are not the work.
#: Kept narrow on purpose: ``development``, ``design`` and ``processing`` are
#: work verbs, while ``management`` alone is usually a wrapper around work.
SUPPORT_WORDS = frozenset(
    {
        "amc",
        "commissioning",
        "control",
        "generation",
        "implementation",
        "installation",
        "integration",
        "maintenance",
        "management",
        "migration",
        "monitoring",
        "operation",
        "operations",
        "repair",
        "repairs",
        "report",
        "reporting",
        "retrieval",
        "service",
        "services",
        "solution",
        "solutions",
        "support",
        "training",
        "upgrade",
    }
)

#: Bare technology, document and delivery words. A phrase made only of these is
#: recall-only: ``web pages`` may find a notice, but it cannot rank one.
GENERIC_WORDS = frozenset(
    {
        "app",
        "apps",
        "application",
        "applications",
        "computer",
        "computers",
        "data",
        "digital",
        "document",
        "documents",
        "hardware",
        "information",
        "it",
        "portal",
        "portals",
        "provide",
        "providing",
        "software",
        "system",
        "systems",
        "technical",
        "technology",
        "technologies",
        "version",
        "web",
        "webpage",
        "webpages",
        "website",
    }
)

_GENERIC_VOCAB = frozenset(NOISE_WORDS) | frozenset(GENERIC_WORK_WORDS) | GENERIC_WORDS


def _tokens(term: str) -> list[str]:
    cleaned = clean_term(term, allow_short_acronym=True)
    if not cleaned:
        return []
    return [
        token
        for token in term_tokens(cleaned)
        if token not in CONNECTORS and token not in {"and", "or", "of", "the"}
    ]


def is_generic(term: str) -> bool:
    """True when the term carries no specific work object."""

    tokens = _tokens(term)
    return bool(tokens) and all(token in _GENERIC_VOCAB for token in tokens)


def is_support(term: str) -> bool:
    """True when the term describes delivery rather than the work itself.

    ``road maintenance`` has a specific object (``road``), so it is work, not
    support. ``technical support`` and ``document retrieval`` have no specific
    object left after the generic words are removed, so they are support.
    """

    tokens = _tokens(term)
    if not tokens:
        return False
    specific = [
        token
        for token in tokens
        if token not in _GENERIC_VOCAB and token not in SUPPORT_WORDS
    ]
    return any(token in SUPPORT_WORDS for token in tokens) and not specific


def classify_term(term: str, *, confirmed: bool = False) -> str:
    """Return ``core``, ``adjacent``, ``support`` or ``generic``.

    ``confirmed`` means a human confirmed the phrase as a capability from the
    company's own document. A confirmed specific phrase is core; the same phrase
    only declared in the profile is adjacent. A support or generic phrase never
    becomes core just because it was confirmed.
    """

    text = normalize_text(term)
    if not text:
        return ROLE_GENERIC
    if is_generic(text):
        return ROLE_GENERIC
    if is_support(text):
        return ROLE_SUPPORT
    return ROLE_CORE if confirmed else ROLE_ADJACENT


def term_text(item) -> str:
    """Read a term from a brief item, a row or a plain string."""

    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("term", "name", "work_family", "title"):
            value = item.get(key)
            if value:
                return str(value)
        return ""
    for attribute in ("name", "work_family", "title"):
        value = getattr(item, attribute, None)
        if value:
            return str(value)
    return ""


def role_of(item, *, confirmed: bool = False) -> str:
    """The stored role when there is one, otherwise a deterministic role."""

    if isinstance(item, dict):
        stored = str(item.get("role") or "").strip().lower()
        if stored in ROLES:
            return stored
    return classify_term(term_text(item), confirmed=confirmed)
