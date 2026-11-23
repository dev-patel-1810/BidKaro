"""Deterministic extraction of tender fields from source text.

Rules:
* Extraction is regex/heuristic based, never model based, so it is reproducible.
* Every extracted field keeps the verbatim excerpt it came from.
* If a field cannot be found it stays ``None`` and is reported as unknown. We do
  not infer or invent authorities, deadlines, values or locations.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlsplit

from app.discovery.models import ExtractedTender

MAX_TEXT_CHARS = 40_000

OFFICIAL_SUFFIXES = (".gov.in", ".nic.in", ".gov", ".mil.in")
OFFICIAL_HOSTS = {
    "eprocure.gov.in",
    "gem.gov.in",
    "gov.in",
    "nic.in",
    "ireps.gov.in",
    "etenders.gov.in",
    "mahatenders.gov.in",
}

# National e-procurement portals, where tender notices are actually published.
# A government body's own site is a different kind of source (an authority).
OFFICIAL_PORTAL_HOSTS = {
    "eprocure.gov.in",
    "gem.gov.in",
    "ireps.gov.in",
    "etenders.gov.in",
    "mahatenders.gov.in",
}

# Drop only well-known tracking parameters when de-duplicating. Anything else in
# the query string may identify the tender itself (e.g. eProcure's ``sp`` token).
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
}

# Aggregator index / search-result pages, not individual tenders.
LISTING_PATTERNS = (
    r"^\s*\d[\d,]*\s+.{0,100}?\btenders\b",
    r"^\s*latest\b.{0,80}?\btenders\b",
    r"\btenders\s+(?:in|from|of|for)\s+(?:india|[a-z][a-z ]{2,30})\b",
    r"\btender\s+notices\b",
    r"\blist\s+of\b.{0,60}?\btenders\b",
    r"\bbrowse\b.{0,40}?\btenders\b",
    r"^\s*tenders\b",
    r"^\s*tender\s+notices?\b(?!\s*(?:no\.?|number|ref)\b).{0,45}$",
    r"^\s*(?:e-?procurement|government\s+e-?procurement|eprocure|gem|mahatenders|ireps|etenders)\b",
    r"\btenders\b[^|]{0,40}\b20\d{2}\b",
    r"\btenders\b\s*$",
)

INDIAN_STATES = (
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa",
    "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
    "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana",
    "tripura", "uttar pradesh", "uttarakhand", "west bengal", "delhi", "jammu",
    "kashmir", "ladakh", "puducherry", "chandigarh", "andaman", "nicobar",
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_DATE_TOKEN = (
    r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}[-/.](?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[-/.]\d{4}"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+\d{4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})"
)

DEADLINE_LABELS = (
    "last date and time of submission",
    "last date of submission",
    "last date for submission",
    "bid submission end date",
    "bid submission closing date",
    "bid submission end",
    "bid closing date",
    "date of submission of bids",
    "submission deadline",
    "closing date",
    "closes",
    "due date",
    "last date",
    "deadline",
)

# Words that mark a date as the *publishing* date rather than the closing date.
# A publish date is never used as a closing date, otherwise an open tender would
# be discarded because its notice was published weeks ago.
PUBLISH_CONTEXT_LABELS = (
    "published date",
    "publish date",
    "date of publication",
    "published on",
    "published",
    "issue date",
    "issued on",
    "issued",
    "opening date",
    "opened on",
    "opening",
    "clarification start",
    "tender published",
    "released on",
    "uploaded on",
    "uploaded",
)

# Extra closing words that are common in short notices and table headers.
CLOSING_CONTEXT_LABELS = DEADLINE_LABELS + (
    "closing",
    "close",
    "submission end",
    "submission ends",
    "till",
    "upto",
    "up to",
    "bid end date",
    "bid end",
)

# Positive evidence that a notice is not an Indian tender. Only used when no
# Indian marker is present, so an unknown notice is never dropped on a guess.
FOREIGN_MARKERS = (
    "united states",
    "united kingdom",
    "usa",
    "u.s.a",
    "uae",
    "dubai",
    "abu dhabi",
    "singapore",
    "saudi arabia",
    "saudi",
    "australia",
    "canada",
    "germany",
    "france",
    "italy",
    "spain",
    "netherlands",
    "belgium",
    "switzerland",
    "qatar",
    "kuwait",
    "bahrain",
    "oman",
    "sri lanka",
    "bangladesh",
    "myanmar",
    "south africa",
    "nigeria",
    "kenya",
    "egypt",
    "turkey",
    "israel",
    "iran",
    "iraq",
    "brazil",
    "mexico",
    "japan",
    "south korea",
    "china",
    "hong kong",
    "taiwan",
    "malaysia",
    "indonesia",
    "philippines",
    "thailand",
    "vietnam",
    "new zealand",
)

FOREIGN_CURRENCY_MARKERS = ("usd", "gbp", "eur", "aed", "sgd", "cad", "aud", "jpy")

VALUE_LABELS = (
    "estimated cost",
    "estimated value",
    "tender value",
    "contract value",
    "project cost",
    "bid value",
    "total value",
    "estimated amount",
)

VALUE_TOKEN = (
    r"(?:INR|RS\.?|₹)\s*([\d][\d,]*(?:\.\d+)?)\s*(crore|cr|lakh|lac|lacs|thousand|k)?"
    r"|([\d][\d,]*(?:\.\d+)?)\s*(crore|cr|lakh|lac|lacs|thousand)\b"
)

AUTHORITY_LABELS = (
    "organisation chain",
    "organization chain",
    "organisation name",
    "organization name",
    "tender inviting authority",
    "inviting authority",
    "issuing authority",
    "name of the department",
    "department name",
    "authority name",
    "authority",
    "organisation",
    "organization",
    "department",
)

# Reference numbers are only taken from explicit labels with a word boundary so
# prose such as "Tender Notices" is never misread as a reference. A portal
# writes "Tender Reference Number<TAB>04/SE/Shahdara Cir. M/PWD/NIT/2026-27",
# so the label the portal actually uses comes first and its value may contain
# spaces; the short forms stay as fallbacks.
REFERENCE_PATTERNS = (
    r"\btender\s+reference\s*(?:no|number|num)?\b\.?\s*[:\-–—]?\s*([^\n\r\t]{3,80})",
    r"\btender\s+ref(?:erence)?\.?\s*(?:no|number)?\b\.?\s*[:\-–—]?\s*([^\n\r\t]{3,80})",
    r"\bref(?:erence)?\.?\s*(?:no|number)\b\.?\s*[:\-–—]?\s*([^\n\r\t]{3,80})",
    r"\b(?:tender|bid|nit|rfp|rfq|eoi)\s*(?:no|number|ref(?:erence)?)\b\.?\s*[:#\-–—]?\s*([A-Za-z0-9][A-Za-z0-9/\-\.]{2,40})",
    r"\bref(?:erence)?\s*(?:no|number)?\b\.?\s*[:#\-–—]\s*([A-Za-z0-9][A-Za-z0-9/\-\.]{2,40})",
)

# A portal tender list writes "[Tender Name] [Ref.No][Tender ID]", so a cell
# sometimes arrives with the reference still in it and no label at all. Only a
# group that looks like a reference is taken: it must carry a digit and a
# slash/dash and must not be a bare number or a date.
_BRACKET_REFERENCE = re.compile(r"\[([^\[\]\n]{4,80})\]")
_BARE_NUMBER = re.compile(r"[\d\s./-]+")


# Social platforms and forums never publish official tender documents, so they
# are excluded before any scoring or fetching happens.
BLOCKED_HOSTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "reddit.com",
    "quora.com",
    "t.me",
    "whatsapp.com",
)

# Award, result and cancellation notices are not open opportunities.
AWARD_PATTERNS = (
    r"\bawarded\b",
    r"\baward\s+of\s+(?:contract|work|tender)\b",
    r"\bletter\s+of\s+award\b",
    r"\bsuccessful\s+bidder\b",
    r"\b(?:tender|bid)\s+results?\b",
    r"\bcancell?ed\b",
    r"\bcancellation\s+of\s+(?:tender|bid)\b",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def is_stated_in_text(value: str | None, text: str | None) -> bool:
    """True when the text itself states this value.

    This is the check that keeps an agent's answer honest. An agent may report
    a closing date or an authority that the page never shows; unless the value
    is found in the text, it is not confirmed and must not be presented as
    fact.
    """
    needle = normalize_text(value).casefold()
    if not needle:
        return False
    haystack = normalize_text(text).casefold()
    if not haystack:
        return False
    if len(needle) > 120:
        # A long paragraph only counts when the page opens with the same words.
        return haystack.startswith(needle[:120]) or needle[:120] in haystack
    return needle in haystack


def stated_excerpt(value: str | None, text: str | None) -> str | None:
    """The line of the text the value was read from, when it is there."""
    if not value:
        return None
    needle = normalize_text(value).casefold()
    if not needle:
        return None
    for line in (text or "").splitlines():
        if needle in normalize_text(line).casefold():
            return line.strip()[:300]
    return None


def _clean(value: str) -> str:
    return normalize_text(value).strip(" :-–—\t\r\n").strip()


def normalize_tender_id(value: str | None) -> str | None:
    """A portal tender id with its markdown escapes removed.

    TinyFish returns page text as markdown, which escapes the underscores in
    ``2026_NHAI_295179_1``. The browser hands over plain text, so the same
    tender id arrives in two spellings and the two reads would never meet.
    """
    cleaned = (value or "").replace("\\_", "_").replace("\\", "").strip()
    return cleaned or None


#: Words a page uses for its own navigation or for a section heading. They are
#: never the name of a place, so a value that carries one was not read from a
#: location field at all.
_CHROME_WORDS = (
    "tenders",
    "tender",
    "organisation",
    "organization",
    "results",
    "home",
    "login",
    "log in",
    "sign in",
    "search",
    "screen reader",
    "archive",
    "cancelled",
    "announcements",
    "awards",
    "downloads",
    "site map",
    "menu",
    "sort by",
    "view more",
    "click here",
    "not found",
)

#: Words that make a value an organisation rather than a place. A list row's
#: organisation-chain column holds both, so the two are told apart before a
#: chain segment is shown as a tender's location.
_ORG_WORDS = (
    "authority",
    "corporation",
    "limited",
    "ltd",
    "department",
    "ministry",
    "office",
    "pvt",
    "division",
    "cell",
    "university",
    "institute",
    "college",
    "board",
    "council",
    "government",
    "commission",
    "society",
    "municipal",
    "trust",
)

#: Extra words a place name may carry once its state is removed: "New Delhi",
#: "North 24 Parganas", "Mumbai City".
_PLACE_FILLER = {
    "and",
    "central",
    "city",
    "distt",
    "district",
    "east",
    "nagar",
    "new",
    "north",
    "rural",
    "south",
    "urban",
    "west",
}


def _looks_like_chrome(value: str) -> bool:
    """True when a value is the page's own navigation, not a fact about a tender."""
    lowered = normalize_text(value).casefold()
    if not lowered:
        return True
    return any(word in lowered for word in _CHROME_WORDS)


def is_page_chrome(value: str | None) -> bool:
    """True when a value is page furniture rather than something the page states.

    "Tenders by Organisation" sits under the label "Location" on every page of
    the portal, so a substring check would happily confirm it as a citation.
    The fields a page states are never its own navigation.
    """
    return _looks_like_chrome(value or "")


def _looks_like_place(value: str) -> bool:
    """True only for a value that is a place name and nothing else.

    A raw cell is read this way rather than by "does it contain a state", which
    accepted ``P6 Delhi - MoRTH`` and ``Delhi University`` - a chain segment and
    an organisation - as the tender's location.
    """
    text = _clean(value)
    if not text or len(text) > 60 or "||" in text:
        return False
    if _looks_like_chrome(text):
        return False
    lowered = text.casefold()
    remainder = lowered
    matched = False
    for state in sorted(INDIAN_STATES, key=len, reverse=True):
        if state in remainder:
            remainder = remainder.replace(state, " ")
            matched = True
    if "india" in remainder:
        remainder = remainder.replace("india", " ")
        matched = True
    if not matched:
        return False
    leftover = [word for word in re.split(r"[^a-z]+", remainder) if word]
    return all(word in _PLACE_FILLER for word in leftover)


#: A value a line starts with, e.g. ``Location`` on its own line. The portal
#: puts the label and its value on separate lines, so the next line is read as
#: the value when the label line carries none.
_NEXT_LINE_LABELS = (
    *AUTHORITY_LABELS,
    "location",
    "place of work",
    "work location",
    "pincode",
    "tender id",
    "tender reference number",
    "bid submission end date",
    "published date",
    "bid opening date",
    "tender value",
    "period of work",
    "pre bid meeting place",
    "tender type",
    "form of contract",
    "tender category",
    "bid validity",
)


#: The fields a notice states about itself. A notice summary can put them all
#: on one line, separated by full stops, so a value is cut where the next field
#: begins rather than at the end of the line.
_NOTICE_FIELD_LABELS = (
    "tender id",
    "tender reference number",
    "tender reference",
    "tender no",
    "tender number",
    "issuing organisation",
    "organisation chain",
    "publishing date",
    "published date",
    "bid submission end date",
    "bid submission start date",
    "document download end date",
    "document download start date",
    "document download/sale end date",
    "document download/sale start date",
    "bid opening date",
    "opening date",
    "closing date and time",
    "pre bid meeting place",
    "pre bid meeting date",
    "pre bid meeting time",
    "estimated cost",
    "tender value",
    "location",
    "pincode",
    "bid validity period",
    "eligibility criteria",
    "documents to be submitted",
    "earnest money",
    "product category",
    "contract type",
    "period of work",
    "tender type",
    "tender category",
    "form of contract",
    "no. of covers",
    "payment mode",
    "work description",
)

#: Field labels that a flattened table row writes as ``label value`` with no
#: colon. A notice read out of the portal's markdown keeps its cells in one
#: string, so "NHAI HQ Pincode 110075" is two fields, not a place called
#: "NHAI HQ Pincode 110075". Only labels listed here may be cut without a
#: colon; these are field names, not words a place or a number would contain.
_FLAT_FIELD_LABELS = (
    "pincode",
    "pre bid meeting place",
    "pre bid meeting date",
    "pre bid meeting time",
    "bid opening date",
    "opening date",
    "document download/sale end date",
    "document download/sale start date",
    "document download end date",
    "document download start date",
    "bid submission end date",
    "bid submission start date",
    "closing date and time",
    "publishing date",
    "published date",
    "tender id",
    "tender reference number",
    "estimated cost",
    "tender value",
    "bid validity period",
    "eligibility criteria",
    "documents to be submitted",
    "product category",
    "contract type",
    "period of work",
    "tender type",
    "tender category",
    "form of contract",
    "no. of covers",
    "payment mode",
    "work description",
)


def _cut_at_next_label(value: str) -> str:
    """Stop a value where the page's next field label begins.

    A one-line notice summary writes its fields as ``Tender Reference Number:
    42/EE/NWR-2/PWD/2026-27. Tender ID: 2026_PWD_300315_1.`` - one paragraph,
    no line breaks. Reading to the end of the line swallowed the tender id and
    the publishing date into the reference number.

    A flattened table row writes the next field as ``label value`` with no
    colon, so a second, narrower cut handles the field names themselves
    (``NHAI HQ Pincode 110075`` is a place and a pincode).
    """
    if not value:
        return value
    labels = sorted(
        {*AUTHORITY_LABELS, *VALUE_LABELS, *DEADLINE_LABELS, *_NOTICE_FIELD_LABELS},
        key=len,
        reverse=True,
    )
    pattern = re.compile(
        r"\s*[.,;]?\s+(?=(?:" + "|".join(re.escape(label) for label in labels) + r")\b[ \t]*[:\-–—])",
        re.IGNORECASE,
    )
    cut = pattern.split(value, maxsplit=1)[0]
    flat = sorted(_FLAT_FIELD_LABELS, key=len, reverse=True)
    flat_pattern = re.compile(
        r"[.,;]?\s+(?=(?:" + "|".join(re.escape(label) for label in flat) + r")\b[ \t]+(?=[^\s]))",
        re.IGNORECASE,
    )
    return flat_pattern.split(cut, maxsplit=1)[0]


def _labelled_value(
    text: str, labels: tuple[str, ...]
) -> tuple[str, str] | None:
    """Read ``label`` and its value the way the portal lays them out.

    Two layouts are handled and nothing else:

    * the label on its own line, with the value on the next line (a portal's
      markdown table row collapses to this);
    * ``label: value`` on one line.

    The old reader allowed the value to start on any later line, which is how
    "Location" was answered by the portal's own navigation text ("Tenders by
    Organisation") two lines below.
    """
    if not text:
        return None
    lowered_labels = tuple(label.casefold() for label in labels)
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        for label in labels:
            head = re.match(
                rf"^{re.escape(label)}\b[ \t]*[:\-–—]?[ \t]*(.*)$", line, re.IGNORECASE
            )
            if not head:
                continue
            value = _clean(head.group(1))
            excerpt = line
            if not value:
                following = _clean(lines[index + 1]) if index + 1 < len(lines) else ""
                if following and following.casefold() not in lowered_labels:
                    if not following.casefold().startswith(
                        tuple(f"{known}:" for known in lowered_labels)
                    ):
                        value = following
                        excerpt = following
            if value:
                return _cut_at_next_label(value), excerpt[:300]
    # One-line layout: "Organisation Chain: Public Works Department||...".
    for label in labels:
        match = re.search(
            rf"\b{re.escape(label)}\b[ \t]*[:\-–—][ \t]*([^\n\r|]{{3,160}})",
            text,
            re.IGNORECASE,
        )
        if match:
            value = _clean(match.group(1))
            if value:
                return _cut_at_next_label(value), _clean(match.group(0))[:300]
    return None


#: What a dead or broken page says. A link the portal refuses - the old
#: /nicgep/ application answers "Not Found" - carries a title and a closing
#: date from the list row that linked to it, so it would otherwise become a
#: tender with nothing but a title. The browser reports such a page as unread;
#: keeping the same rule here means the two cannot disagree.
_ERROR_PAGE_MARKERS = (
    "requested url was not found",
    "404 not found",
    "page not found",
    "http status 404",
    "service unavailable",
    "bad gateway",
    # A notice address carries the walk's session. Reopened cold, the portal
    # answers with a short timeout page, which is not a notice and must send
    # the fetch back through the locator instead of being read as one.
    "session has timed out",
    "session timed out",
    "session in the client area has expired",
    "your session has expired",
)


def is_error_page(text: str) -> bool:
    """True when the page is an error page rather than a notice.

    A notice is a long, structured page. The portal's failure pages are a
    sentence long, so the check is deliberately narrow: a long page is never
    thrown away for containing a word.
    """
    lowered = (text or "").strip().casefold()
    if not lowered:
        return True
    if len(lowered) > 600:
        return False
    return any(marker in lowered for marker in _ERROR_PAGE_MARKERS)


def _host(url: str) -> str:
    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def is_listing_page(title: str) -> bool:
    """Detect aggregator index/search pages that are not an individual tender.

    Only plural "tenders" phrasing and explicit index patterns are treated as
    listings, so a real title such as "Road construction tender in Maharashtra"
    is not filtered out.
    """
    text = normalize_text(title).lower()
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in LISTING_PATTERNS)


def is_blocked_host(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS)


def is_award_notice(title: str, snippet: str = "") -> bool:
    """True for award, result or cancellation notices rather than open tenders."""
    text = normalize_text(f"{title} {snippet}").lower()
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in AWARD_PATTERNS)


#: The portal's own statement that this tender's bidding cycle is over. The
#: navigation link "Cancelled/Retendered" is on every page, so a bare
#: ``cancelled`` is never enough: the pattern has to name the tender or the
#: cycle. A cancelled tender is not a tender with changed fields; it is a
#: cycle that ended, and the interface says so instead of showing its details.
TERMINATION_PATTERNS = (
    r"current\s+bidding\s+cycle\s+(?:is|has\s+been)\s+terminated",
    r"bidding\s+cycle\s+(?:is|has\s+been)\s+terminated",
    r"completely\s+new\s+process\s+will\s+be\s+initiated",
    r"tender\s+(?:has\s+been|is)\s+cancell?ed",
    r"cancellation\s+of\s+(?:the\s+)?(?:tender|bid)",
    r"tender\s+cancell?ed\s+corrigendum",
    r"tender\s+retendere?d\s+corrigendum",
    r"bid\s+(?:has\s+been|is)\s+cancell?ed",
    r"(?:tender|bid)\s+(?:has\s+been|is)\s+terminated",
)

#: The one sentence the interface shows when the portal states the cycle is
#: over. It is fixed: no new date, no new reference and no invented reason is
#: added to a tender whose source does not state one.
TERMINATION_MESSAGE = (
    "The current bidding cycle is terminated. A completely new process will "
    "be initiated."
)


def termination_statement(text: str | None) -> str | None:
    """The line where the portal states the cycle is over, if it does.

    The line is returned verbatim so it can be kept as the evidence for the
    change. Nothing is inferred from a missing notice or a passed deadline:
    only a sentence that names the tender or its bidding cycle counts.
    """

    for line in (text or "").splitlines():
        cleaned = normalize_text(line)
        if len(cleaned) < 8:
            continue
        if any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in TERMINATION_PATTERNS):
            return cleaned[:300]

    # A flattened page can put the statement on one long line. Read the
    # sentence around the match rather than handing over the whole page.
    whole = normalize_text(text)
    for pattern in TERMINATION_PATTERNS:
        match = re.search(pattern, whole, re.IGNORECASE)
        if not match:
            continue
        start = max(whole.rfind(".", 0, match.start()) + 1, 0)
        end = whole.find(".", match.end())
        sentence = whole[start : end + 1 if end != -1 else len(whole)].strip()
        return (sentence or match.group(0))[:300]
    return None


def is_non_tender(title: str, snippet: str = "") -> bool:
    return is_listing_page(title) or is_award_notice(title, snippet)


# --------------------------------------------------------------- listing tables
# Government portals often render tenders as an HTML table. TinyFish returns it
# as a markdown table, so individual tenders can be ingested from the page text.

NAME_HEADERS = (
    "tender name",
    "name of work",
    "work name",
    "tender",
    "work",
    "description",
    "subject",
    "title",
    "particulars",
)
CLOSE_HEADERS = (
    "closing date",
    "closing",
    "last date",
    "due date",
    "submission",
    "end date",
    "deadline",
)
PUBLISH_HEADERS = (
    "publish",
    "published",
    "start date",
    "date of publication",
    "upload",
    "issue date",
)
_DATE_IN_CELL = re.compile(
    r"(\d{1,2}[\s,./-][A-Za-z]{3,9}[\s,./-]\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})"
)
_TABLE_SEPARATOR = re.compile(r"^:?-{2,}:?$")


@dataclass(slots=True)
class TableRowTender:
    title: str
    deadline: str | None
    deadline_raw: str | None
    published: str | None
    excerpt: str
    #: What the row states besides its dates. A portal list row carries the
    #: reference number, the tender id and the organisation chain; dropping
    #: them is what left a Discover card with a title and nothing else.
    reference_number: str | None = None
    tender_id: str | None = None
    authority: str | None = None
    location: str | None = None


def _table_cells(line: str) -> list[str]:
    return [_clean(cell) for cell in line.strip().strip("|").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(_TABLE_SEPARATOR.match(cell.replace(" ", "")) for cell in cells)


def _column_index(headers: list[str], terms: tuple[str, ...], *, exclude: set[int]) -> int | None:
    for index, header in enumerate(headers):
        if index in exclude:
            continue
        lowered = header.lower()
        if any(term in lowered for term in terms):
            return index
    return None


_TENDER_ID_RE = re.compile(r"\b(20\d{2}_[A-Za-z]{2,}[A-Za-z0-9_]*_\d+_\d+)\b")

#: Some portals number a notice with a plain long number instead of the
#: ``2026_DEPT_123_1`` form. It is only ever read from a field labelled
#: "Tender ID", never guessed from prose.
_NUMERIC_TENDER_ID = re.compile(r"\b(\d{6,})\b")

#: Words that make a short cell an organisation rather than a place of work.
_CORPORATE_WORDS = (
    "authority",
    "corporation",
    "limited",
    "ltd",
    "department",
    "ministry",
    "office",
    "pvt",
    "division",
    "cell",
)


def _single_date(cell: str) -> tuple[date, str] | None:
    match = _DATE_IN_CELL.search(cell or "")
    if not match:
        return None
    parsed = parse_date_token(match.group(1))
    return (parsed, match.group(1)) if parsed else None


def _split_title_cell(cell: str) -> tuple[str, str | None, str | None]:
    """Separate a list cell's bracketed groups into name, reference and id.

    A portal writes the work, the reference number and the tender id in the
    same cell: ``[Name] [Ref][2026_NHAI_295179_1]``. The name is the first
    group; the id is the group that matches the portal's id shape; the
    reference is the other identifier. Nothing is invented when a group is
    missing.
    """
    groups = [
        group.replace("\\_", "_").strip() for group in re.findall(r"\[([^\]]*)\]", cell)
    ]
    if not groups:
        return cell, None, None
    reference: str | None = None
    tender_id: str | None = None
    for group in groups[1:]:
        if tender_id is None and _TENDER_ID_RE.fullmatch(group):
            tender_id = group
            continue
        if reference is None and re.search(r"\d", group) and ("/" in group or "-" in group):
            reference = group
    if tender_id is None:
        match = _TENDER_ID_RE.search(cell.replace("\\_", "_"))
        tender_id = match.group(1) if match else None
    if reference is None:
        reference = tender_id
    return groups[0], reference, tender_id


def _row_extras(
    cells: list[str], *, name_index: int
) -> tuple[str | None, str | None, str | None, str | None]:
    """Read the reference, the tender id, the place and the authority a row states."""
    reference: str | None = None
    tender_id: str | None = None
    location: str | None = None
    authority: str | None = None
    for index, cell in enumerate(cells):
        if index == name_index:
            continue
        cleaned = cell.replace("\\_", "_")
        match = _TENDER_ID_RE.search(cleaned)
        if match and tender_id is None:
            tender_id = match.group(1)
        # The organisation-chain column is the row's authority, and its first
        # segment is the body that issued the tender.
        if authority is None and "||" in cell:
            first = _clean(cell.split("||", 1)[0])
            if first and len(first) <= 200:
                authority = first
        short = len(cell) < 120
        if (
            reference is None
            and short
            and not _DATE_IN_CELL.search(cell)
            and _looks_like_reference(cell)
            and re.search(r"[/-]", cell)
            and re.search(r"\d", cell)
        ):
            reference = cell
        if location is None and _looks_like_place(cell):
            location = cell
    if reference is None:
        reference = tender_id
    return (
        reference[:200] if reference else None,
        tender_id,
        _clean(location)[:120] if location else None,
        authority,
    )


def _row_from_cppp_cells(
    cells: list[str], raw_row: str, *, today: date
) -> TableRowTender | None:
    """Read one CPPP tender-list row when the table has no usable header.

    The portal's list is a fixed six-column shape: S.No, e-Publish Date,
    Closing Date, Opening Date, "Title, Ref.No./Tender ID", Organisation Chain.
    The markdown conversion drops the heading cells, so the columns are read by
    position.

    Two things here are deliberate. The closing date is the **middle** date:
    the last one is the opening date, and taking it as the closing date tells
    the user they have days they do not have. And the title, its reference and
    its tender id all sit in the same bracketed cell, so they are separated
    here instead of being thrown away.
    """
    if len(cells) < 5 or not re.fullmatch(r"\d{1,4}", cells[0].strip()):
        return None
    published = _single_date(cells[1])
    closing = _single_date(cells[2])
    opening = _single_date(cells[3])
    if not (published and closing and opening):
        return None
    # The portal writes them in this order; any other order is another table.
    if not (published[0] <= closing[0] <= opening[0]):
        return None
    title_cell = cells[4]
    if not title_cell.startswith("["):
        return None
    title, reference, tender_id = _split_title_cell(title_cell)
    if len(title) < 8:
        return None

    authority = next((cell for cell in cells[5:] if cell.strip()), None)
    if authority:
        authority = _clean(authority.split("||", 1)[0])[:200] or None
    return TableRowTender(
        title=title[:400],
        deadline=closing[0].isoformat(),
        deadline_raw=closing[1],
        published=published[0].isoformat(),
        excerpt=_clean(raw_row)[:400],
        reference_number=reference[:200] if reference else None,
        tender_id=normalize_tender_id(tender_id),
        authority=authority,
    )


def extract_table_rows(
    text: str, *, max_rows: int = 12, today: date | None = None
) -> list[TableRowTender]:
    """Pull individual tenders out of markdown table listings.

    Only rows with a name and a parseable closing date in the future are kept,
    so tables of organisations, counts or past results are ignored.
    """
    if not text:
        return []
    current_day = today or date.today()
    lines = [line.strip() for line in text.splitlines()]
    rows: list[TableRowTender] = []
    index = 0
    while index < len(lines) and len(rows) < max_rows:
        if not (lines[index].startswith("|") and lines[index].count("|") >= 3):
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and lines[index].startswith("|") and lines[index].count("|") >= 3:
            block.append(lines[index])
            index += 1
        if len(block) < 2:
            continue

        headers = _table_cells(block[0])
        close_idx = _column_index(headers, CLOSE_HEADERS, exclude=set())
        publish_idx = _column_index(
            headers, PUBLISH_HEADERS, exclude={close_idx} if close_idx is not None else set()
        )
        name_idx = _column_index(
            headers,
            NAME_HEADERS,
            exclude={position for position in (close_idx, publish_idx) if position is not None},
        )
        if close_idx is None or name_idx is None:
            # No usable headings. Many portals and aggregators publish their
            # tables without column names, so each row is read positionally.
            for raw_row in block[1:]:
                cells = _table_cells(raw_row)
                if _is_separator_row(cells):
                    continue
                inferred = _row_from_cppp_cells(
                    cells, raw_row, today=current_day
                ) or infer_row_tender(cells, today=current_day)
                if inferred:
                    rows.append(inferred)
                    if len(rows) >= max_rows:
                        break
            continue

        for raw_row in block[1:]:
            cells = _table_cells(raw_row)
            if _is_separator_row(cells):
                continue
            if max(name_idx, close_idx) >= len(cells):
                continue
            # A heading table can still put the name, the reference and the
            # tender id in the one cell, so the cell is split the same way.
            title, cell_reference, cell_tender_id = _split_title_cell(cells[name_idx])
            if not title or len(title) < 15 or title.lower() in {"tender name", "name of work"}:
                continue
            match = _DATE_IN_CELL.search(cells[close_idx])
            if not match:
                continue
            deadline = parse_date_token(match.group(1))
            if deadline is None or deadline < current_day:
                continue
            published = None
            if publish_idx is not None and publish_idx < len(cells):
                published_match = _DATE_IN_CELL.search(cells[publish_idx])
                if published_match:
                    published = parse_date_token(published_match.group(1))
            reference, tender_id, location, authority = _row_extras(cells, name_index=name_idx)
            reference = cell_reference or reference
            tender_id = cell_tender_id or tender_id
            if location is None:
                location = _state_in(title)
            # The organisation-chain column states the authority, and its first
            # segment is the body that issued the tender.
            if authority is None:
                chain_idx = _column_index(headers, ("organisation chain", "organization chain"), exclude=set())
                if chain_idx is not None and chain_idx < len(cells):
                    chain = cells[chain_idx]
                    if chain:
                        authority = _clean(chain.split("||", 1)[0])[:200] or None
            rows.append(
                TableRowTender(
                    title=title[:400],
                    deadline=deadline.isoformat(),
                    deadline_raw=match.group(1),
                    published=published.isoformat() if published else None,
                    excerpt=_clean(raw_row)[:400],
                    reference_number=reference,
                    tender_id=normalize_tender_id(tender_id),
                    authority=authority,
                    location=location,
                )
            )
            if len(rows) >= max_rows:
                break
    return rows


def split_list_cell(cell: str) -> tuple[str, str | None, str | None]:
    """Public form of the list-cell split: (work name, reference, tender id).

    A sweep reads a tender from its listing row rather than its notice page, so
    it needs exactly this split and must agree with the walk about which
    bracketed group is the name and which is the identifier.
    """

    return _split_title_cell(cell)


def tender_identity_key(tender_id: str | None, fallback: str) -> str:
    """The key a tender is stored under, so one tender is one record.

    The portal's tender id is the stable identifier: it is the same whether the
    tender was read out of a list row or opened as a notice, and the same in
    every browser session. A notice address carries the session it was opened
    in, so keying on the address alone puts the same tender on the page twice -
    once from the list and once from the notice - with one card full and one
    card empty.
    """
    cleaned = normalize_tender_id(tender_id)
    if cleaned and (
        _TENDER_ID_RE.fullmatch(cleaned) or _NUMERIC_TENDER_ID.fullmatch(cleaned)
    ):
        return hashlib.sha256(f"tender:{cleaned}".encode("utf-8")).hexdigest()
    return fallback


def notice_identity(text: str) -> tuple[str | None, str | None]:
    """The tender id and publishing date a notice states about itself.

    A list page carries the tender id inside its title cell, but a notice page
    carries it in a field of its own. Reading it here is what lets the page a
    search fetched and the same page a browser opened be one record.
    """
    tender_id: str | None = None
    found = _labelled_value(text, ("tender id", "tender number", "tender no"))
    if found:
        cleaned = normalize_tender_id(found[0]) or ""
        match = _TENDER_ID_RE.search(cleaned)
        if match:
            tender_id = match.group(1)
        else:
            numeric = _NUMERIC_TENDER_ID.search(cleaned)
            if numeric:
                tender_id = numeric.group(0)
    published: str | None = None
    found = _labelled_value(
        text, ("publishing date", "published date", "publish date", "publication date")
    )
    if found:
        published = _clean(found[0])[:60] or None
    return tender_id, published


def row_dedupe_key(url: str, title: str) -> str:
    base = f"{dedupe_key(url, title)}::{normalize_text(title).lower()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def is_detail_url(url: str) -> bool:
    """True when the URL looks like an individual record rather than a section root."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 3:
        return True
    return len(segments) == 2 and len(segments[1]) >= 20


def has_many_dated_rows(text: str, *, minimum: int = 4) -> bool:
    """True when the page is mostly a dated table, i.e. a listing rather than one tender."""
    if not text:
        return False
    dated = 0
    for line in text.splitlines():
        if line.count("|") >= 3 and _DATE_IN_CELL.search(line):
            dated += 1
            if dated >= minimum:
                return True
    return False


def classify_source(url: str, site_name: str = "") -> str:
    host = _host(url)
    if not host:
        return "unknown"
    if host in OFFICIAL_HOSTS or host.endswith(OFFICIAL_SUFFIXES):
        return "official"
    return "aggregator"


def dedupe_key(url: str, title: str, site_name: str = "") -> str:
    """Stable per-company key so the same tender updates rather than duplicates.

    Meaningful query parameters are kept because several official portals encode
    the tender id in the query string. Common tracking parameters are dropped.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/").lower()
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query.sort()
    query_string = "&".join(f"{key}={value}" for key, value in query)
    if path or query_string:
        base = f"{host}{path}" + (f"?{query_string}" if query_string else "")
    else:
        base = f"{host}:{normalize_text(title).lower()}|{normalize_text(site_name).lower()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def parse_date_token(token: str) -> date | None:
    token = _clean(token).replace(",", " ").replace(".", " ")
    token = re.sub(r"\s+", " ", token).strip()
    # dd mm yyyy or dd-mm-yyyy
    match = re.fullmatch(r"(\d{1,2})[-/ ](\d{1,2})[-/ ](\d{2,4})", token)
    if match:
        day, month, year = (int(part) for part in match.groups())
        year = _expand_year(year)
        return _safe_date(year, month, day)
    # dd Mon yyyy
    match = re.fullmatch(r"(\d{1,2})[-/.]([a-z]+)[-/.](\d{4})", token.lower())
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name)
        if month:
            return _safe_date(int(year), month, int(day))
    # dd Mon yyyy
    match = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", token.lower())
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name)
        if month:
            return _safe_date(int(year), month, int(day))
    # Mon dd yyyy
    match = re.fullmatch(r"([a-z]+)\s+(\d{1,2})\s+(\d{4})", token.lower())
    if match:
        month_name, day, year = match.groups()
        month = _MONTHS.get(month_name)
        if month:
            return _safe_date(int(year), month, int(day))
    # yyyy-mm-dd
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", token)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return _safe_date(year, month, day)
    return None


# ---------------------------------------------------------------------------
# Closing-date rules
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DateFacts:
    """Closing and publish dates read out of a short piece of source text."""

    deadline: date | None = None
    published: date | None = None
    deadline_raw: str | None = None
    context: str = "unknown"  # how the closing date was identified


def _date_tokens(text: str) -> list[tuple[int, str, date]]:
    found: list[tuple[int, str, date]] = []
    for match in re.finditer(_DATE_TOKEN, text, re.IGNORECASE):
        parsed = parse_date_token(match.group(1))
        if parsed:
            found.append((match.start(), match.group(1), parsed))
    return found


def _date_context(text: str, index: int) -> str:
    """Classify a date by the words immediately before it."""
    window = text[max(0, index - 70) : index].lower()
    best_kind = "unknown"
    best_position = -1
    for labels, kind in (
        (PUBLISH_CONTEXT_LABELS, "publish"),
        (CLOSING_CONTEXT_LABELS, "closing"),
    ):
        for label in labels:
            position = window.rfind(label)
            if position > best_position:
                best_position = position
                best_kind = kind
    return best_kind


def read_close_date(text: str, *, today: date | None = None) -> DateFacts:
    """Read the closing date out of a short notice, row or snippet.

    Rules, in order:

    1. A date with a closing/submission word next to it is the closing date.
    2. A date with a publish/open word next to it is never the closing date.
    3. If no date has a closing word, the latest remaining date is used as the
       closing date and the earlier one is kept as the publish date.
    4. A notice that only states a publish date has no closing date at all.
    """
    current_day = today or date.today()
    if not text:
        return DateFacts()
    tokens = _date_tokens(text)
    if not tokens:
        return DateFacts()

    closing = [item for item in tokens if _date_context(text, item[0]) == "closing"]
    published = [item for item in tokens if _date_context(text, item[0]) == "publish"]
    unlabelled = [item for item in tokens if _date_context(text, item[0]) == "unknown"]

    facts = DateFacts()
    if closing:
        _index, raw, parsed = closing[0]
        facts.deadline = parsed
        facts.deadline_raw = raw
        facts.context = "closing"
    elif unlabelled:
        _index, raw, parsed = max(unlabelled, key=lambda item: item[2])
        facts.deadline = parsed
        facts.deadline_raw = raw
        facts.context = "inferred"
    if published:
        facts.published = min(item[2] for item in published)
    if facts.published is None and facts.deadline is not None:
        earlier = [item[2] for item in tokens if item[2] < facts.deadline]
        if earlier:
            facts.published = min(earlier)
    if facts.deadline is not None and facts.deadline < current_day:
        # Expired. The caller decides what to do with it, but we never report an
        # expired date as a usable closing date.
        facts.context = f"{facts.context}:expired"
    return facts


def looks_like_single_notice(text: str) -> bool:
    """True when the page states a closing date for a single work."""
    if not text:
        return False
    lowered = text.lower()
    return any(label in lowered for label in CLOSING_CONTEXT_LABELS)


def is_notice_page(text: str) -> bool:
    """True when the page is one tender notice rather than a list of tenders.

    A notice states its own identity: a ``Tender ID`` and a ``Tender Reference
    Number``, next to a closing date for submission. A tender *list* has a
    column headed "Title, Ref.No./Tender ID" and no reference-number field of
    its own, so it does not pass this test.

    This matters because a notice page carries tables of its own - fee
    instruments, covers, the document checklist - and reading those rows as
    tenders is what put a card called "Ministry of Road Transport and
    Highways" next to the very notice it came from.
    """
    if not text:
        return False
    lowered = text.lower()
    if not any(label in lowered for label in CLOSING_CONTEXT_LABELS):
        return False
    return "tender reference number" in lowered and (
        "tender id" in lowered or "tender details" in lowered
    )


_GENERIC_TITLE_ENDINGS = ("tender", "tenders", "tender notice", "e-tender", "bid notice")

# Section headings a page uses for its own data, never a description of work.
_LABEL_PHRASE_STARTS = (
    "tender opening",
    "tender closing",
    "tender date",
    "tender amount",
    "tender title",
    "tender value",
    "tender type",
    "tender fee",
    "bid opening",
    "bid closing",
    "bid submission",
    "bid end",
    "bid start",
    "document download",
    "download start",
    "download end",
    "opening date",
    "closing date",
    "last date",
    "first date",
    "published date",
    "publish date",
    "amended date",
    "corrigendum",
    "extension",
    "e-tender",
    "etender",
)


def _looks_like_label_phrase(text: str) -> bool:
    """True for a section heading such as "Tender Opening Date"."""
    lowered = normalize_text(text).lower().strip(" .:|-")
    if not lowered:
        return True
    return lowered.startswith(_LABEL_PHRASE_STARTS)


def is_generic_title(title: str) -> bool:
    """True when a title names an authority or a portal, not a piece of work.

    Aggregators publish many pages called "... Tender" or "Authority - Tender".
    Those titles are useless on a card, so the work description has to come
    from the page itself.
    """
    text = normalize_text(title).lower().strip(" .-|")
    if not text:
        return True
    if _looks_like_label_phrase(text):
        return True
    words = text.split()
    if len(words) <= 8:
        for ending in _GENERIC_TITLE_ENDINGS:
            if text.endswith(ending):
                return True
    if len(text) < 15:
        return True
    return False


def work_name_from_text(text: str) -> str | None:
    """Read a work description from the top of a page.

    The first meaningful line of a notice is its title, e.g.
    "Pune Road Surface Marking Project for Enhanced Safety".
    """
    if not text:
        return None
    for raw_line in text.splitlines():
        line = _clean(raw_line).lstrip("#").strip()
        if len(line) < 20 or len(line) > 200:
            continue
        if line.endswith(":"):
            continue
        if re.search(r"https?://", line, re.IGNORECASE):
            continue
        if _DATE_IN_CELL.search(line) and len(line) < 40:
            continue
        lowered = line.lower()
        if any(
            lowered.startswith(prefix)
            for prefix in (
                "tender no",
                "tender reference",
                "organisation",
                "organization",
                "department",
                "authority",
                "last date",
                "closing date",
                "bid submission",
                "opening date",
                "estimated cost",
                "tender value",
                "work location",
            )
        ):
            continue
        if any(label in lowered for label in DEADLINE_LABELS[:6]):
            continue
        if _looks_like_label_phrase(line):
            continue
        return line
    return None


def is_foreign_notice(text: str) -> bool:
    """True only when the text positively points to a notice outside India."""
    if not text:
        return False
    lowered = text.lower()
    indian_markers = ("india", "inr", "lakh", "crore", "lakhs", "crores", "rupees")
    indian_markers += INDIAN_STATES
    if any(marker in lowered for marker in indian_markers):
        return False
    for marker in FOREIGN_MARKERS:
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            return True
    for marker in FOREIGN_CURRENCY_MARKERS:
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            return True
    return False


def _looks_like_reference(cell: str) -> bool:
    """A cell that is only an identifier, not a work description."""
    cleaned = _clean(cell)
    if not cleaned:
        return False
    if len(cleaned) > 40:
        return False
    digits = sum(character.isdigit() for character in cleaned)
    if digits >= 6 and digits >= len(cleaned) * 0.5:
        return True
    return bool(_REFERENCE_CELL_RE.search(cleaned))


_REFERENCE_CELL_RE = re.compile(
    r"\b(?:tender|bid|nit|rfp|rfq|eoi)\s*(?:no|number|ref(?:erence)?)\b", re.IGNORECASE
)


def _looks_like_value(cell: str) -> bool:
    return bool(re.search(VALUE_TOKEN, cell, re.IGNORECASE))


def _looks_like_location(cell: str) -> bool:
    """A cell that names a place rather than describing the work."""
    lowered = cell.lower()
    if "india" in lowered:
        return True
    return any(state in lowered for state in INDIAN_STATES)


def _state_in(text: str) -> str | None:
    """The first Indian state a piece of text names, title-cased.

    Used only for a tender's own work description: the place a tender is for is
    the state its work names ("... in the State of Goa ..."), and the earliest
    one named is the one the work is about.
    """
    lowered = (text or "").casefold()
    best: tuple[int, str] | None = None
    for state in sorted(INDIAN_STATES, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(state)}\b", lowered)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), state)
    return best[1].title() if best else None


def infer_row_tender(cells: list[str], *, today: date | None = None) -> TableRowTender | None:
    """Read one tender out of a table row that has no usable header.

    Many aggregator and government tables are published without column headings.
    The row is read positionally instead: the closing date is the latest date in
    the row, the work description is the longest piece of text, and the
    reference is the cell that is only an identifier.
    """
    current_day = today or date.today()
    if len(cells) < 2:
        return None

    dated: list[tuple[int, date, str]] = []
    for index, cell in enumerate(cells):
        for match in _DATE_IN_CELL.finditer(cell):
            parsed = parse_date_token(match.group(1))
            if parsed:
                dated.append((index, parsed, match.group(1)))
    if not dated:
        return None
    future = [item for item in dated if item[1] >= current_day]
    if not future:
        return None
    close_index, close_date, close_raw = max(future, key=lambda item: item[1])
    published = min((item[1] for item in dated if item[1] < close_date), default=None)

    name = ""
    name_index = -1
    for index, cell in enumerate(cells):
        if index == close_index:
            continue
        # A cell is metadata only when it is short. A work description can name
        # a state ("... in Delhi"), a date ("... dt 19.10.2026") or an amount
        # without stopping being the work description, and skipping it is what
        # turned the organisation chain into the tender's title.
        if len(cell) < 40 and _DATE_IN_CELL.search(cell):
            continue
        if _looks_like_reference(cell):
            continue
        if len(cell) < 40 and _looks_like_value(cell):
            continue
        if len(cell) < 40 and _looks_like_location(cell):
            continue
        if len(cell) > len(name):
            name = cell
            name_index = index
    # A work description is a sentence, not a column heading.
    if len(name) < 25 or _looks_like_label_phrase(name):
        return None
    if name.startswith("["):
        name, cell_reference, cell_tender_id = _split_title_cell(name)
        if len(name) < 25:
            return None
    else:
        cell_reference = cell_tender_id = None

    reference, tender_id, location, authority = _row_extras(cells, name_index=name_index)
    reference = cell_reference or reference
    tender_id = cell_tender_id or tender_id
    # The row's own cells rarely carry a place, but the work names one: "...
    # in the State of Goa ..." is where the tender is.
    if location is None:
        location = _state_in(name)
    return TableRowTender(
        title=name[:400],
        deadline=close_date.isoformat(),
        deadline_raw=close_raw,
        published=published.isoformat() if published else None,
        excerpt=_clean(" | ".join(cells))[:400],
        reference_number=reference,
        tender_id=normalize_tender_id(tender_id),
        authority=authority,
        location=location,
    )


@dataclass(slots=True)
class SnippetNotice:
    """One tender read out of a search result snippet."""

    title: str
    deadline: str | None
    deadline_raw: str | None
    published: str | None
    reference_number: str | None
    excerpt: str
    context: str = "unknown"


def notice_from_snippet(title: str, snippet: str, *, today: date | None = None) -> SnippetNotice | None:
    """Read one tender out of a search result snippet.

    A search result often describes a single notice even when the page it
    points at is a category page. The work description is the text before the
    closing date and the closing date follows the same rules as a table row.
    """
    current_day = today or date.today()
    if not snippet:
        return None
    facts = read_close_date(snippet, today=current_day)
    if facts.deadline is None or facts.deadline < current_day:
        return None

    # The work description is whatever precedes the closing date.
    head = snippet
    if facts.deadline_raw:
        position = snippet.find(facts.deadline_raw)
        if position > 0:
            head = snippet[:position]
    for separator in ("·", "|", "•"):
        if separator in head:
            head = head.split(separator, 1)[0]
    # Drop the closing word itself, e.g. "... Under Ward No. 47. Closes".
    head = re.sub(
        r"\b(?:closing date|closes|close|due date|deadline|last date)\b\s*[:\-]?\s*$",
        "",
        head,
        flags=re.IGNORECASE,
    )
    name = _clean(head).strip(" ,.;:-—–")
    if len(name) < 15:
        return None
    if is_listing_page(name) or _looks_like_label_phrase(name):
        # The snippet only describes a category or a section, not one work.
        return None

    reference = _extract_reference(snippet)
    return SnippetNotice(
        title=name[:400],
        deadline=facts.deadline.isoformat(),
        deadline_raw=facts.deadline_raw,
        published=facts.published.isoformat() if facts.published else None,
        reference_number=reference[0] if reference else None,
        excerpt=_clean(snippet)[:400],
        context=facts.context,
    )


def source_kind(url: str) -> str:
    """Classify where a page comes from.

    * ``official_portal`` - a national e-procurement portal.
    * ``authority`` - a government body's own site.
    * ``aggregator`` - a commercial third-party listing site.
    """
    host = _host(url)
    if not host:
        return "unknown"
    if host in OFFICIAL_HOSTS or host in OFFICIAL_PORTAL_HOSTS:
        return "official_portal"
    if host.endswith(OFFICIAL_SUFFIXES):
        return "authority"
    return "aggregator"


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year < 70 else 1900 + year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _search_labeled(
    text: str, labels: tuple[str, ...], value_pattern: str
) -> tuple[str, str] | None:
    """Find ``label value`` on the label's own line and return (value, excerpt).

    The value never starts on a later line: a page's navigation sits a line or
    two below its labels, and reading across that boundary is how a tender's
    location became "Tenders by Organisation".
    """
    for label in labels:
        pattern = re.compile(
            rf"\b{re.escape(label)}\b[ \t]*[:\-–—]?[ \t]*({value_pattern})",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if match:
            raw = _clean(match.group(1))
            if raw:
                return raw, _clean(match.group(0))
    return None


def _extract_authority(text: str) -> tuple[str, str] | None:
    """Read the issuing authority the page states.

    A portal writes it as ``Organisation Chain`` followed by the department
    hierarchy: ``National Highways Authority of India||RO-Gandhinagar - NHAI``.
    The authority is the first segment; the whole chain is not a name. The
    label may sit on its own line with the value below it, which is how the
    fetched markdown of a notice page looks.
    """
    found = _labelled_value(text, AUTHORITY_LABELS)
    if not found:
        return None
    raw, excerpt = found
    value = re.split(r"\s{2,}|\t", raw)[0]
    if "||" in value:
        value = value.split("||", 1)[0]
    value = _clean(value)
    if 3 <= len(value) <= 120:
        return value, excerpt
    return None



def _extract_deadline(text: str) -> tuple[date, str, str] | None:
    for label in DEADLINE_LABELS:
        pattern = re.compile(
            rf"\b{re.escape(label)}\b[^\d\n\r]{{0,40}}?({_DATE_TOKEN})",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if match:
            parsed = parse_date_token(match.group(1))
            if parsed:
                return parsed, _clean(match.group(1)), _clean(match.group(0))
    # The portal writes the label on its own line and the date below it, so a
    # same-line-only reader loses the closing date of every fetched notice.
    for label in DEADLINE_LABELS:
        found = _labelled_value(text, (label,))
        if not found:
            continue
        value, excerpt = found
        token = re.search(_DATE_TOKEN, value, re.IGNORECASE)
        if not token:
            continue
        parsed = parse_date_token(token.group(1))
        if parsed:
            return parsed, _clean(token.group(1)), _clean(excerpt)[:300]
    return None


def parse_value(raw: str, *, currency: str = "INR") -> tuple[float, str, str] | None:
    """Read a tender value out of a piece of text, without needing a label.

    When the text carries a currency marker or a unit, that is used. When the
    caller already knows the currency from the label it read - the portal
    states "Tender Value in Rs" and then the bare number - a bare number is
    taken as that currency. Returns (amount, raw text, excerpt) or None.
    """
    found = _extract_value(raw, require_label=False)
    if found:
        return found
    number = re.search(r"([\d][\d,]*(?:\.\d+)?)", raw or "")
    if not number:
        return None
    digits = number.group(1).replace(",", "")
    if not digits:
        return None
    try:
        amount = float(digits)
    except ValueError:
        return None
    # "Tender Value in Rs: 0.00" and "NA" are the page saying it has no value,
    # not a tender worth nothing.
    if amount <= 0:
        return None
    return amount, number.group(1), normalize_text(raw)[:300]


def _value_from_notice_line(text: str) -> tuple[float, str, str] | None:
    """Read the value off the line that states it, the way the portal writes it.

    The portal writes ``Tender Value in Rs<TAB>3,38,81,600``. The words "in Rs"
    sit between the label and the number, so the labelled reader cannot see the
    amount; the first real number on that line is the value.
    """
    lines = (text or "").splitlines()
    labels = ("tender value", "estimated cost", "estimated value", "contract value")
    for index, line in enumerate(lines):
        lowered = line.casefold()
        label = next((name for name in labels if name in lowered), None)
        if not label:
            continue
        # Only the text after the label belongs to this field: a one-line
        # summary puts the reference and the tender id before it, and reading
        # the whole line made the tender id's own digits the tender's value.
        tail = _cut_at_next_label(line[lowered.find(label) + len(label) :])
        # The value is usually on the same line; a page that breaks the label
        # and the number into two lines puts it on the next one.
        for candidate in (tail, lines[index + 1] if index + 1 < len(lines) else ""):
            if not candidate.strip():
                continue
            stated_zero = False
            for raw in re.findall(r"[\d][\d,]*(?:\.\d+)?", candidate):
                digits = raw.replace(",", "")
                try:
                    amount = float(digits)
                except ValueError:
                    continue
                if amount <= 0:
                    stated_zero = True
                    continue
                # A lone small number is a year or a percentage, not a value.
                if "," not in raw and amount < 10_000:
                    continue
                return amount, raw, normalize_text(line)[:300]
            # The field states a zero ("Tender Value in Rs 0.00"): the page is
            # saying the tender has no value, so the next line is another
            # field's value and not this one.
            if stated_zero:
                return None
    return None


def _extract_value(text: str, *, require_label: bool = True) -> tuple[float, str, str] | None:
    if require_label:
        result = _search_labeled(text, VALUE_LABELS, VALUE_TOKEN)
        if result:
            raw, excerpt = result
            amount = _parse_amount(raw)
            if amount is not None:
                return amount, raw, excerpt
        return _value_from_notice_line(text)

    # Fallback: an explicit currency amount with a unit, e.g. "₹ 43.90 Crore".
    # Only ever applied to the short title/snippet, never to a whole fetched page.
    match = re.search(
        r"(?:INR|Rs\.?|₹)\s*[\d][\d,]*(?:\.\d+)?\s*(?:crore|cr|lakh|lac|lacs|thousand)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    raw = _clean(match.group(0))
    amount = _parse_amount(raw)
    if amount is None:
        return None
    return amount, raw, raw


def _extract_reference(text: str) -> tuple[str, str] | None:
    for pattern in REFERENCE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = _clean(match.group(1))
            # A value read to the end of a line can carry the next column's
            # prose; a table gap or a pipe ends it.
            value = _clean(re.split(r"\s{2,}|\|", value)[0])
            # A one-line summary puts the next field on the same line, so the
            # reference ends where that field's label begins.
            value = _clean(_cut_at_next_label(value))
            value = re.sub(r"[.,;:\s]+$", "", value)
            # Reference numbers in practice contain at least one digit; rejecting
            # prose keeps labels like "Tender Notices" from inventing a reference.
            if value and re.search(r"\d", value):
                return value, _clean(match.group(0))[:300]
    return _extract_bracketed_reference(text)


def _extract_bracketed_reference(text: str) -> tuple[str, str] | None:
    """A reference that a list cell states in brackets, with no label."""
    for match in _BRACKET_REFERENCE.finditer(text):
        value = _clean(match.group(1))
        if not value or not re.search(r"\d", value):
            continue
        if "/" not in value and "-" not in value:
            continue
        if _BARE_NUMBER.fullmatch(value):
            continue
        if len(value.split()) > 12:
            continue
        return value, _clean(match.group(0))[:300]
    return None


def _extract_location(text: str, brief: str) -> tuple[str, str] | None:
    """Read the place of work the page states under a location label.

    A labelled value is trusted as written (a portal's own office address is a
    real location), but the page's navigation is not: "Tenders by Organisation"
    sits under the label "Location" on every page of the portal, and showing it
    as a tender's location is worse than showing nothing.
    """
    labeled = _labelled_value(text, ("location", "place of work", "work location"))
    if labeled:
        value = re.sub(r"[.\s]+$", "", labeled[0]).strip()
        if value and len(value) <= 80 and not _looks_like_chrome(value):
            return value, labeled[1]
    # Fall back to a state named in the tender's own title/snippet, never to a
    # state merely mentioned somewhere in the fetched page.
    lowered = brief.lower()
    best: tuple[int, str] | None = None
    for state in INDIAN_STATES:
        idx = lowered.find(state)
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, state)
    if best is None:
        return None
    idx, state = best
    return state.title(), _clean(brief[max(0, idx - 20) : idx + len(state) + 20])


def extract_fields(title: str, snippet: str, page_text: str = "") -> ExtractedTender:
    brief = "\n".join(part for part in (title, snippet) if part)
    text = "\n".join(part for part in (title, snippet, (page_text or "")[:MAX_TEXT_CHARS]) if part)
    result = ExtractedTender()
    evidence: dict[str, str] = {}

    authority = _extract_authority(text)
    if authority:
        result.authority = authority[0]
        evidence["authority"] = authority[1][:300]

    reference = _extract_reference(text)
    if reference:
        result.reference_number = reference[0]
        evidence["reference_number"] = reference[1]

    deadline = _extract_deadline(text)
    if deadline:
        parsed, raw, excerpt = deadline
        result.deadline = parsed.isoformat()
        result.deadline_raw = raw
        evidence["deadline"] = excerpt[:300]

    value = _extract_value(text) or _extract_value(brief, require_label=False)
    if value:
        amount, raw, excerpt = value
        result.value_amount = amount
        result.value_currency = "INR"
        result.value_raw = raw
        evidence["value"] = excerpt[:300]

    location = _extract_location(text, brief)
    if location:
        result.location = location[0]
        evidence["location"] = location[1][:300]

    result.evidence = evidence
    return result


def _parse_amount(raw: str) -> float | None:
    match = re.search(VALUE_TOKEN, raw, re.IGNORECASE)
    if not match:
        return None
    amount_str = match.group(1) or match.group(3)
    unit = (match.group(2) or match.group(4) or "").lower()
    if not amount_str:
        return None
    try:
        amount = float(amount_str.replace(",", ""))
    except ValueError:
        return None
    if unit in {"crore", "cr"}:
        amount *= 10_000_000
    elif unit in {"lakh", "lac", "lacs"}:
        amount *= 100_000
    elif unit in {"thousand", "k"}:
        amount *= 1_000
    # A stated zero is the page saying it has no value, not a tender worth
    # nothing: "Tender Value in Rs 0.00" must not read as ₹0.
    if amount <= 0:
        return None
    return amount


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
