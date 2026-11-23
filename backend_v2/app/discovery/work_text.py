"""What a notice's words say about the *work*, and what they do not.

A tender title is written for a buyer, not for a matcher: it carries the buyer's
name, the place, the department and a reference code around the one phrase that
says what has to be done. Matching the whole title against a company profile is
therefore mostly matching proper nouns, which is how a scanning firm ends up
"relevant" to a public works department because the department's name contains
"technical".

This module reduces a notice to the work phrase:

* bracket groups and portal reference codes are removed;
* a trailing ``for/at/in/under`` organisation or place is cut off;
* a leading department/project name is cut off;
* place names, buyer words and short organisation acronyms are dropped, while a
  buyer word that is also a kind of work (``data centre``, ``college
  building``) survives inside the work phrase;
* a small set of boilerplate words (``computer``, ``document``, ``software``,
  ``entry``, ``high`` ...) is never work evidence on its own.

The gazetteers are *not* a work vocabulary. They only name the words that are
about a buyer, a place or a document rather than a kind of work. There is no
list of approved work types, so the matcher stays usable for civil work, a
manufacturer, a trader, a software firm and a consultancy alike.
"""

from __future__ import annotations

import re

#: Grammar words. They join a phrase but never carry work meaning.
CONNECTORS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "per",
        "the",
        "to",
        "under",
        "upto",
        "via",
        "with",
        "within",
    }
)

#: Places and administrative regions. A place is where the work is, not what
#: it is. The list is deliberately about geography, not about sectors.
PLACE_WORDS = frozenset(
    {
        "andhra",
        "arunachal",
        "assam",
        "bihar",
        "chandigarh",
        "chhattisgarh",
        "delhi",
        "goa",
        "gujarat",
        "haryana",
        "himachal",
        "hyderabad",
        "india",
        "indian",
        "jammu",
        "kashmir",
        "karnataka",
        "kerala",
        "ladakh",
        "lakshadweep",
        "madhya",
        "maharashtra",
        "manipur",
        "meghalaya",
        "mizoram",
        "mumbai",
        "nagaland",
        "odisha",
        "orissa",
        "puducherry",
        "punjab",
        "rajasthan",
        "sikkim",
        "tamil",
        "telangana",
        "tripura",
        "uttar",
        "uttarakhand",
        "west",
        "bengal",
        "bengaluru",
        "bangalore",
        "chennai",
        "kolkata",
        "pune",
        "nagpur",
        "nashik",
        "ahmedabad",
        "surat",
        "jaipur",
        "lucknow",
        "kanpur",
        "patna",
        "bhopal",
        "indore",
        "raipur",
        "ranchi",
        "bhubaneswar",
        "guwahati",
        "shillong",
        "imphal",
        "aizawl",
        "kohima",
        "gangtok",
        "dehradun",
        "shimla",
        "jammu",
        "srinagar",
        "leh",
        "port",
        "blair",
        "panaji",
        "thiruvananthapuram",
        "kochi",
        "coimbatore",
        "madurai",
        "visakhapatnam",
        "vijayawada",
        "vadodara",
        "rajkot",
        "ludhiana",
        "amritsar",
        "jodhpur",
        "udaipur",
        "agra",
        "varanasi",
        "meerut",
        "noida",
        "gurugram",
        "gurgaon",
        "faridabad",
        "ghaziabad",
        "jabalpur",
        "gwalior",
        "aurangabad",
        "solapur",
        "kolhapur",
        "tirupati",
        "mysuru",
        "mysore",
        "mangaluru",
        "kozhikode",
        "thrissur",
        "warangal",
        "guntur",
        "nellore",
        "tiruchirappalli",
        "salem",
        "vellore",
        "hubballi",
        "belagavi",
        "dharwad",
        "silchar",
        "dibrugarh",
        "jorhat",
        "tezpur",
        "bareilly",
        "aligarh",
        "moradabad",
        "saharanpur",
        "gorakhpur",
        "prayagraj",
        "allahabad",
        "jhansi",
        "bikaner",
        "kota",
        "ajmer",
        "bhilwara",
        "alwar",
        "sikar",
        "pali",
        "bharatpur",
    }
)

#: Words that describe a buyer, an office or a project owner. They are useful
#: for cutting a title, never for matching work. Some are also kinds of work
#: (``data centre``, ``college building``); :data:`WORK_WORD_ORG` says which
#: ones survive in the middle of a work phrase.
ORG_WORDS = frozenset(
    {
        "academy",
        "authority",
        "board",
        "body",
        "bureau",
        "centre",
        "center",
        "chairman",
        "chief",
        "college",
        "commission",
        "committee",
        "company",
        "corporation",
        "corp",
        "council",
        "court",
        "department",
        "dept",
        "directorate",
        "division",
        "foundation",
        "government",
        "govt",
        "headquarters",
        "hq",
        "institute",
        "institution",
        "limited",
        "ltd",
        "ministry",
        "municipal",
        "municipality",
        "nagar",
        "office",
        "panchayat",
        "parishad",
        "pradesh",
        "private",
        "pvt",
        "samiti",
        "society",
        "trust",
        "tribunal",
        "university",
        "zilla",
        # Organisation/project acronyms a profile document tends to harvest.
        # A short acronym is not a kind of work; it is a name.
        "aiims",
        "bhel",
        "bmrcl",
        "cpwd",
        "dcte",
        "dmrc",
        "drdo",
        "gail",
        "iit",
        "iim",
        "iiit",
        "ircon",
        "isro",
        "jjm",
        "mahsar",
        "mmrda",
        "nhai",
        "nhpc",
        "nit",
        "npcil",
        "ongc",
        "pwd",
        "rites",
        "sjvn",
    }
)

#: Organisation words that are also a kind of work. They are still buyer clues
#: at the start or after "for", but inside a phrase ("data centre", "college
#: building", "court records") removing them would delete the work itself.
WORK_WORD_ORG = frozenset(
    {
        "academy",
        "centre",
        "center",
        "college",
        "court",
        "foundation",
        "institute",
        "institution",
        "office",
        "university",
    }
)

#: Words that cannot tell one kind of work from another on their own. A phrase
#: such as "document management system" is still meaningful; each of its words
#: alone is not. They are *noise* for a partial match, never a reason to drop a
#: whole phrase.
NOISE_WORDS = frozenset(
    {
        "address",
        "annual",
        "basis",
        "bid",
        "bids",
        "cin",
        "code",
        "computer",
        "computers",
        "condition",
        "conditions",
        "contact",
        "contract",
        "contracts",
        "data",
        "document",
        "documents",
        "email",
        "entry",
        "equipment",
        "etc",
        "goods",
        "gst",
        "high",
        "id",
        "information",
        "item",
        "items",
        "job",
        "jobs",
        "low",
        "material",
        "materials",
        "mobile",
        "mode",
        "modes",
        "month",
        "months",
        "new",
        "no",
        "nos",
        "notice",
        "notices",
        "old",
        "order",
        "orders",
        "pan",
        "part",
        "parts",
        "pdf",
        "page",
        "pages",
        "period",
        "phone",
        "product",
        "products",
        "rate",
        "rates",
        "ref",
        "reference",
        "requirement",
        "requirements",
        "rfp",
        "scope",
        "service",
        "services",
        "sl",
        "software",
        "sr",
        "supplies",
        "supply",
        "system",
        "systems",
        "technical",
        "technology",
        "tender",
        "tenders",
        "terms",
        "type",
        "types",
        "website",
        "work",
        "works",
        "year",
        "years",
    }
)

#: Work nouns/verbs broad enough that seeing one of them in a longer profile
#: phrase is not evidence of that phrase. "Construction" appears in a road
#: tender and in a building tender; it cannot carry "road construction" alone.
GENERIC_WORK_WORDS = frozenset(
    {
        "academy",
        "annual",
        "centre",
        "center",
        "college",
        "consultancy",
        "consulting",
        "construction",
        "contract",
        "court",
        "development",
        "equipment",
        "foundation",
        "goods",
        "hardware",
        "implementation",
        "institute",
        "institution",
        "installation",
        "maintenance",
        "management",
        "manufacturing",
        "material",
        "materials",
        "office",
        "operation",
        "operations",
        "procurement",
        "project",
        "purchase",
        "repair",
        "repairs",
        "service",
        "services",
        "software",
        "solution",
        "solutions",
        "supply",
        "supplies",
        "system",
        "systems",
        "technology",
        "university",
        "works",
        "work",
    }
)

#: Work verbs/nouns that usually begin the actual title after a department name.
#: This is not a list of approved work: it only tells ``Department of Technical
#: Education Supply of computers`` where the department ends.
WORK_ACTION_WORDS = frozenset(
    {
        "appointment",
        "audit",
        "beautification",
        "booking",
        "catering",
        "cleaning",
        "commissioning",
        "construction",
        "consultancy",
        "consulting",
        "design",
        "development",
        "digitisation",
        "digitization",
        "distribution",
        "drilling",
        "empanelment",
        "engagement",
        "evaluation",
        "event",
        "exploration",
        "fabrication",
        "hiring",
        "housekeeping",
        "implementation",
        "installation",
        "logistics",
        "maintenance",
        "management",
        "manufacturing",
        "mapping",
        "marketing",
        "mining",
        "operation",
        "operations",
        "outsourcing",
        "printing",
        "procurement",
        "provide",
        "providing",
        "provision",
        "purchase",
        "recruitment",
        "renovation",
        "repair",
        "research",
        "running",
        "scanning",
        "security",
        "selection",
        "supplies",
        "supply",
        "supplying",
        "survey",
        "surveying",
        "training",
        "transportation",
        "upgrade",
        "upgradation",
    }
)

#: A short, cross-sector set of acronyms that name a kind of work. It is not a
#: work vocabulary: it only translates an acronym a person uses into the phrase
#: a title uses, and it never makes an organisation acronym (NHAI, DCTE) into
#: work evidence.
ACRONYM_EXPANSIONS = {
    "amc": "annual maintenance contract",
    "cctv": "closed circuit television",
    "dms": "document management system",
    "erp": "enterprise resource planning",
    "gis": "geographic information system",
    "gps": "global positioning system",
    "hvac": "heating ventilation and air conditioning",
    "ocr": "optical character recognition",
}

#: Only these short acronyms survive :func:`clean_term` when they come from a
#: declared keyword. A confirmed alias is always allowed, because a human wrote
#: it for exactly this purpose.
WORK_ACRONYMS = frozenset(ACRONYM_EXPANSIONS)

_BRACKET_RE = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_CODE_RE = re.compile(
    r"\b(?:[a-z]{0,8}\d{2,}[a-z0-9]*|\d{4,}[a-z0-9]*|\d+)\b", re.IGNORECASE
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ORG_TAIL_RE = re.compile(
    r"\b(?:for|at|in|under|by|to|on\s+behalf\s+of)\b", re.IGNORECASE
)


def normalize_text(value: str | None) -> str:
    """Lowercase words with punctuation and hyphenation flattened to spaces."""

    text = (value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def words(value: str | None) -> list[str]:
    return normalize_text(value).split()


def tokens(value: str | None) -> list[str]:
    return _TOKEN_RE.findall(normalize_text(value))


def singular(token: str) -> str:
    """A small, deterministic singular form, enough for plural/word-order use."""

    word = token
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def strip_brackets(value: str | None) -> str:
    return _BRACKET_RE.sub(" ", value or "")


def _is_org_acronym(token: str) -> bool:
    """A short all-capitals word that is not a known work acronym."""

    return (
        2 <= len(token) <= 6
        and token.isalpha()
        and token.isupper()
        and token.lower() not in WORK_ACRONYMS
    )


def _drop_leading_org(text: str) -> str:
    """Remove ``Technical Education Department supply of ...`` style prefixes."""

    raw = words(text)
    if not raw:
        return ""
    # Only a title that *starts* with an organisation can have a leading org
    # prefix. In "digitisation of court records" the word "court" is part of
    # what is being digitised, not the buyer, so cutting at it would throw the
    # work away.
    if raw[0] not in ORG_WORDS:
        return " ".join(raw)
    limit = min(6, len(raw))
    cut_at = -1
    for index in range(limit):
        token = raw[index]
        if token in ORG_WORDS:
            cut_at = index
    if cut_at >= 0:
        if cut_at + 1 < len(raw):
            remainder = raw[cut_at + 1 :]
            # "Department of Technical Education Supply of computers": the
            # department word is at the front, so jump to the first work action
            # rather than leaving "of technical education" behind.
            if cut_at == 0 and raw[0] in {
                "department",
                "office",
                "ministry",
                "government",
                "directorate",
                "municipal",
                "municipality",
                "authority",
                "board",
                "corporation",
                "commission",
                "council",
                "university",
                "college",
                "institute",
            }:
                for index, token in enumerate(remainder):
                    if token in WORK_ACTION_WORDS:
                        return " ".join(remainder[index:])
            return " ".join(remainder)
        return ""
    return " ".join(raw)


def _cut_org_tail(text: str) -> str:
    """Cut a ``for the Department of ...`` / ``in Maharashtra`` tail.

    The cut only happens when the tail names a buyer or a place. ``survey for
    highway widening`` keeps its tail because ``highway`` is the work, while
    ``survey for NHAI`` loses the buyer.
    """

    for match in _ORG_TAIL_RE.finditer(text):
        tail = text[match.end() :].strip()
        if not tail:
            continue
        tail_words = words(tail)
        head = tail_words[:8]
        connector = match.group(0).lower()
        starts_org = bool(head) and (
            head[0] in ORG_WORDS
            or head[0] in PLACE_WORDS
            or head[0] in {"department", "government", "govt", "ministry", "office"}
        )
        names_org = any(
            word in ORG_WORDS or word in PLACE_WORDS for word in head
        )
        if starts_org or (connector != "for" and names_org):
            return text[: match.start()].strip()
        if connector == "for" and names_org:
            # "for Government College" and "for the Department of ..." are
            # buyers; "for highway widening" is the work.
            return text[: match.start()].strip()
    return text


def clean_term(value: str | None, *, allow_short_acronym: bool = False) -> str | None:
    """Reduce one profile phrase to its work words, or ``None`` if it is not work.

    A confirmed alias is allowed to be a short acronym (``DMS``); a harvested
    or declared term is not, unless the acronym is one of the small work
    acronyms above. This is what keeps ``MAHSR`` and ``DCTE`` out without
    stopping a document-management firm from using ``DMS``.
    """

    raw = strip_brackets(value).strip()
    if not raw:
        return None

    # Drop organisation acronyms from a phrase before normalising, because the
    # case information is what tells "NHAI" from a work word.
    raw_parts = [part for part in re.split(r"\s+", raw) if part]
    if len(raw_parts) == 1 and _is_org_acronym(raw_parts[0]):
        if not allow_short_acronym:
            return None
    elif len(raw_parts) > 1:
        raw = " ".join(
            part for part in raw_parts if not _is_org_acronym(part)
        )

    cleaned = normalize_text(raw)
    parts = [part for part in cleaned.split() if part]
    while parts and parts[0] in CONNECTORS:
        parts.pop(0)
    while parts and parts[-1] in CONNECTORS:
        parts.pop()
    if not parts:
        return None

    if len(parts) == 1:
        term = parts[0]
        if term in PLACE_WORDS or term in ORG_WORDS or term in NOISE_WORDS:
            return None
        if term in GENERIC_WORK_WORDS and term not in WORK_ACRONYMS:
            # A single "construction" or "services" is a category, not a kind
            # of work. It would match almost every notice.
            return None
        return term if len(term) >= 2 else None

    # A phrase made only of places, buyers and joining words is a name, not
    # work. "Government of India" and "Jammu and Kashmir" are not capabilities.
    if all(
        part in PLACE_WORDS or part in ORG_WORDS or part in CONNECTORS
        for part in parts
    ):
        return None

    # Trim a place/buyer at either end while a work phrase remains.
    while len(parts) > 2 and (parts[0] in PLACE_WORDS or parts[0] in ORG_WORDS):
        parts.pop(0)
    while len(parts) > 2 and (parts[-1] in PLACE_WORDS or parts[-1] in ORG_WORDS):
        parts.pop()
    # Cutting the buyer can expose a joining word ("Department of Road
    # Transport" -> "of road transport"); keep the work words only.
    while parts and parts[0] in CONNECTORS:
        parts.pop(0)
    while parts and parts[-1] in CONNECTORS:
        parts.pop()
    if not parts:
        return None
    if all(part in PLACE_WORDS or part in ORG_WORDS for part in parts):
        return None
    cleaned = " ".join(parts)
    return cleaned if len(cleaned) >= 3 else None


def _drop_codes(text: str) -> str:
    return _CODE_RE.sub(" ", text)


def work_text(*parts: str | None) -> str:
    """Return only the words that describe the work in a title or snippet."""

    raw = " ".join(str(part) for part in parts if part)
    if not raw:
        return ""
    text = strip_brackets(raw)
    # A pipe/semicolon usually starts a portal's metadata tail.
    text = re.split(r"\s*[|;•·]\s*", text)[0]
    text = _cut_org_tail(text)
    text = _drop_leading_org(text)
    text = _drop_codes(text)
    cleaned_words = []
    for word in words(text):
        if word in PLACE_WORDS:
            continue
        # A buyer word in the middle of a title is still not work ("Public
        # Works Department Housekeeping"), but a word that names a kind of work
        # too must survive ("data centre", "college building", "court
        # records"). The leading/trailing cuts above already removed the buyer
        # name in those positions.
        if word in ORG_WORDS and word not in WORK_WORD_ORG:
            continue
        if word in CONNECTORS:
            # Keep a connector only between work words; a dangling one would
            # make a phrase look different from the same phrase written plainly.
            cleaned_words.append(word)
            continue
        cleaned_words.append(word)
    while cleaned_words and cleaned_words[0] in CONNECTORS:
        cleaned_words.pop(0)
    while cleaned_words and cleaned_words[-1] in CONNECTORS:
        cleaned_words.pop()
    return " ".join(cleaned_words)


def _spelling_variants(text: str) -> list[str]:
    """British/American spellings that name the same work."""

    out = [text]
    for british, american in (
        ("isation", "ization"),
        ("isations", "izations"),
        ("ise", "ize"),
        ("ised", "ized"),
        ("ising", "izing"),
    ):
        if british in text:
            out.append(text.replace(british, american))
        if american in text:
            out.append(text.replace(american, british))
    return out


def term_variants(term: str) -> list[str]:
    """Deterministic variants of one profile term for a contains-prefilter."""

    base = normalize_text(term)
    if not base:
        return []
    variants: list[str] = [base]
    if not base.endswith("s"):
        if base.endswith(("s", "x", "z", "ch", "sh")):
            variants.append(f"{base}es")
        else:
            variants.append(f"{base}s")
    space_words = base.split()
    if len(space_words) == 2:
        variants.append(f"{space_words[1]} of {space_words[0]}")
        plural = (
            f"{space_words[1]}s of {space_words[0]}"
            if not space_words[1].endswith("s")
            else f"{space_words[1]} of {space_words[0]}"
        )
        variants.append(plural)
    for variant in list(variants):
        variants.extend(_spelling_variants(variant))
    seen: set[str] = set()
    unique: list[str] = []
    for variant in variants:
        if variant not in seen:
            seen.add(variant)
            unique.append(variant)
    return unique


def contains(haystack: str, phrase: str) -> bool:
    pattern = re.escape(normalize_text(phrase)).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", haystack))


def term_tokens(term: str) -> list[str]:
    return [token for token in normalize_text(term).split() if token not in CONNECTORS]


def _all_tokens_present(term: str, work_tokens: list[str]) -> bool:
    term_parts = term_tokens(term)
    if not term_parts:
        return False
    work_singular = {singular(token) for token in work_tokens}
    return all(singular(token) in work_singular for token in term_parts)


def _acronym_expansions_for(term: str) -> list[str]:
    base = normalize_text(term)
    expansions: list[str] = []
    if base in ACRONYM_EXPANSIONS:
        expansions.append(ACRONYM_EXPANSIONS[base])
    for acronym, expansion in ACRONYM_EXPANSIONS.items():
        if base == expansion:
            expansions.append(acronym)
    return expansions


def term_match(work: str, term: str) -> tuple[str, str | None] | None:
    """Match one profile term against a work text.

    Returns ``("full", None)`` for the whole phrase, ``("partial", word)`` when
    only a specific word of a longer phrase is present, or ``None``. A full
    match is phrase-or-word-order independent: ``data processing`` matches
    ``processing of data``. A partial match never uses a noise word such as
    ``document`` or a generic word such as ``construction``, so a road
    contractor is not matched by every construction notice.
    """

    text = normalize_text(work)
    if not text:
        return None
    term_words = term_tokens(term)
    if not term_words:
        return None
    work_tokens = tokens(text)

    # Exact variants first (including singular/plural and "B of A").
    for variant in term_variants(term):
        if contains(text, variant):
            return ("full", None)

    # The same phrase with its words in another order: "processing of data"
    # for "data processing", "construction of road" for "road construction".
    if len(term_words) >= 2 and _all_tokens_present(term, work_tokens):
        return ("full", None)

    # A known acronym and its expansion are the same phrase.
    for expansion in _acronym_expansions_for(term):
        expansion_tokens = tokens(expansion)
        if not expansion_tokens:
            continue
        if _all_tokens_present(expansion, work_tokens):
            return ("full", None)
        if _all_tokens_present(term, expansion_tokens):
            return ("full", None)

    if len(term_words) < 2:
        return None

    # Partial: one specific word of a multi-word phrase. Noise and generic
    # words are excluded so the match says something about the work.
    work_singular = {singular(token) for token in work_tokens}
    specific = [
        token
        for token in term_words
        if token not in NOISE_WORDS
        and token not in GENERIC_WORK_WORDS
        and token not in PLACE_WORDS
        and token not in ORG_WORDS
        and token not in CONNECTORS
    ]
    for token in specific:
        if singular(token) in work_singular:
            return ("partial", token)
    return None


def meaningful_tokens(term: str) -> list[str]:
    return [
        token
        for token in term_tokens(term)
        if token not in NOISE_WORDS
        and token not in GENERIC_WORK_WORDS
        and token not in PLACE_WORDS
        and token not in ORG_WORDS
    ]
