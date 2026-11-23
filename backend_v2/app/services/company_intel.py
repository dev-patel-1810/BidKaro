"""Read an ``about_company`` document into work terms.

Deterministic code owns this: the page text comes from the same PDF reader the
tender documents use, and the terms are repeated multi-word phrases plus the
specific words inside them. A phrase keeps the page and the verbatim line it
came from, so the Documents tab can show the evidence and a human can remove a
term the reader got wrong.

Two rules from the product spec shape the harvest:

* a *phrase*, not a bare keyword, is the unit of work. A word only becomes a
  term when it sits inside a phrase the document repeats;
* a client, an authority or an empanelment partner is never work evidence, so
  those segments are removed before anything is counted.

The reader is not an eligibility check. An uploaded profile cannot make a
mandatory criterion a pass; it only tells discovery what kind of work the
company does.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.identity import Company, CompanyDocument
from app.repositories.company_intel import CompanyDocumentIntelRepository
from app.services import storage
from app.services.company_understanding import CompanyUnderstandingService
from app.services.ocr import OcrUnavailable, build_ocr
from app.services.pdf_extraction import PdfExtractionError, extract_pdf_pages

ABOUT_COMPANY_TYPE = "about_company"
#: The harvest rules, not the matcher rules. A stored term carries this stamp
#: so a changed reader can be recognised later; the matcher has its own version.
COMPANY_INTEL_VERSION = "2026-11-17.1"

MAX_PHRASES = 12
MAX_TOKENS = 24
MAX_EXCERPT_CHARS = 400
MIN_PHRASE_OCCURRENCES = 2

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")

#: Words that describe any company or any tender, never a kind of work. This is
#: deliberately a boilerplate list, not a work vocabulary: it never names a
#: kind of work and it never decides what a company does. Grammar words are in
#: it because "for" or "the" cannot be part of a work phrase; company and
#: tender words are in it because "limited" or "government" appear in almost
#: every document and every notice.
_BOILERPLATE = {
    "a", "about", "above", "across", "after", "again", "against", "all", "almost",
    "along", "also", "although", "among", "an", "and", "another", "any", "are",
    "around", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing",
    "done", "down", "during", "each", "either", "else", "etc", "even", "ever",
    "every", "few", "first", "for", "from", "further", "had", "has", "have",
    "having", "he", "her", "here", "hers", "herself", "him", "himself", "his",
    "how", "however", "i", "if", "in", "into", "is", "it", "its", "itself",
    "just", "like", "made", "make", "makes", "many", "may", "me", "might",
    "more", "most", "much", "must", "my", "myself", "near", "need", "needs",
    "no", "nor", "not", "now", "of", "off", "on", "once", "one", "only", "or",
    "other", "others", "our", "ours", "ourselves", "out", "over", "own", "per",
    "perhaps", "please", "same", "she", "should", "since", "so", "some", "such",
    "than", "that", "the", "their", "theirs", "them", "themselves", "then",
    "there", "these", "they", "this", "those", "though", "three", "through",
    "to", "too", "two", "under", "until", "up", "upon", "us", "use", "used",
    "uses", "using", "very", "via", "was", "we", "well", "were", "what", "when",
    "where", "whether", "which", "while", "who", "whom", "whose", "why", "will",
    "with", "within", "without", "would", "yes", "yet", "you", "your", "yours",
    "yourself", "yourselves", "zero",
    # company / document boilerplate
    "address", "best", "bid", "bidding", "bids", "certificate", "certified",
    "certification", "client", "clients", "company", "companies", "contract",
    "contracts", "corporate", "corporation", "customer", "customers", "email",
    "empaneled", "empanelled", "empanelment", "experience", "experienced",
    "government", "govt", "gst", "india", "indian", "iso", "leading", "limited",
    "ltd", "manpower", "mobile", "office", "pan", "phone", "premier", "private",
    "project", "projects", "provider", "providers", "pvt", "quality",
    "registered", "sector", "service", "services", "since", "solution",
    "solutions", "staff", "team", "teams", "tender", "tenders", "top", "trusted",
    "undertaking", "website", "work", "works", "year", "years",
}

_CIN_PAN = re.compile(
    r"\b(?:cin|pan|gstin|gst|tan|udyam|msme)\b\s*[:#-]?\s*[a-z0-9/]{6,}",
    re.IGNORECASE,
)
_EMAIL_OR_URL = re.compile(
    r"(?:https?://|www\.|[\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE
)
_PHONE = re.compile(r"(?:\+?\d[\d\s()/-]{8,}\d)")
_NOISE = re.compile(
    r"\b(?:all rights reserved|disclaimer|confidential|copyright|"
    r"registered office|corporate office|research date|prepared by|"
    r"page\s+\d+\s+of\s+\d+)\b",
    re.IGNORECASE,
)
#: A cue that the rest of the line names a client, not a kind of work. The
#: "customer support" service is not a client list, so the cue needs a colon or
#: a list verb after the word.
_CLIENT_CUE = re.compile(
    r"\b(?:empanel(?:led|ment)?|"
    r"(?:our\s+)?clients?\s*(?::|include|includes|are)|"
    r"customers?\s*(?::|include|includes|are)|"
    r"orders?\s+from|worked\s+with|work\s+done\s+for)\b",
    re.IGNORECASE,
)
_WORK_MARKERS = (
    " relating to ",
    " to provide ",
    " to carry out ",
    " for ",
    " as ",
)


@dataclass(slots=True)
class HarvestedTerm:
    term: str
    page_index: int
    printed_label: str | None
    excerpt: str


def _normalize(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _tokens(line: str) -> list[str]:
    return [match.group(0).strip(".") for match in _TOKEN_RE.finditer(line)]


def _is_content(token: str) -> bool:
    cleaned = token.casefold().strip(".")
    if not cleaned or cleaned.isdigit():
        return False
    if cleaned in _BOILERPLATE:
        return False
    if len(cleaned) < 2:
        return False
    return True


def _strip_clients(line: str) -> str | None:
    """Keep the work part of a client/empanelment line, never the client name."""

    match = _CLIENT_CUE.search(line)
    if match is None:
        return line
    tail = line[match.end() :]
    lowered = f" {tail.casefold()} "
    for marker in _WORK_MARKERS:
        index = lowered.find(marker)
        if index == -1:
            continue
        candidate = tail[index + len(marker) - 1 :].strip()
        if len(_tokens(candidate)) >= 2:
            return candidate
    return None


def _clean_line(
    line: str, *, strip_phrases: tuple[str, ...] = ()
) -> str | None:
    cleaned = " ".join((line or "").replace("\x00", " ").split())
    if len(cleaned) < 4:
        return None
    for phrase in strip_phrases:
        if phrase:
            cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) < 4:
        return None
    if _EMAIL_OR_URL.search(cleaned) or _CIN_PAN.search(cleaned):
        return None
    if _NOISE.search(cleaned):
        return None
    if _PHONE.search(cleaned) and sum(ch.isalpha() for ch in cleaned) < 20:
        return None
    return _strip_clients(cleaned)


def _phrase_candidates(
    lines: list[tuple[int, str | None, str]],
) -> tuple[dict[str, int], dict[str, tuple[int, str | None, str]]]:
    counts: Counter[str] = Counter()
    first: dict[str, tuple[int, str | None, str]] = {}
    for page_index, printed_label, line in lines:
        tokens = _tokens(line)
        # Contiguous content runs: a stop word ends a phrase, so "document
        # scanning and digitization" contributes "document scanning" and
        # nothing across the "and".
        runs: list[list[str]] = []
        current: list[str] = []
        for token in tokens:
            if _is_content(token):
                current.append(token)
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)
        for run in runs:
            for size in (2, 3, 4):
                if size > len(run):
                    break
                for start in range(len(run) - size + 1):
                    words = run[start : start + size]
                    if not any(len(word) >= 5 for word in words):
                        continue
                    phrase = _normalize(" ".join(words))
                    if len(phrase) < 8:
                        continue
                    counts[phrase] += 1
                    first.setdefault(phrase, (page_index, printed_label, line))
    return dict(counts), first


def _select_phrases(
    counts: dict[str, int], first: dict[str, tuple[int, str | None, str]]
) -> list[str]:
    ranked = sorted(
        (phrase for phrase, count in counts.items() if count >= MIN_PHRASE_OCCURRENCES),
        key=lambda phrase: (
            counts[phrase] * len(phrase),
            counts[phrase],
            len(phrase.split()),
        ),
        reverse=True,
    )
    # A phrase that is only a piece of a longer repeated phrase adds nothing to
    # the reasons, so keep the fuller one.
    selected: list[str] = []
    for phrase in sorted(ranked, key=lambda item: len(item.split()), reverse=True):
        if any(f" {phrase} " in f" {longer} " for longer in selected):
            continue
        selected.append(phrase)
    selected.sort(
        key=lambda phrase: (counts[phrase] * len(phrase), counts[phrase]),
        reverse=True,
    )
    return selected[:MAX_PHRASES]


def _fallback_phrases(
    lines: list[tuple[int, str | None, str]],
    counts: dict[str, int],
    first: dict[str, tuple[int, str | None, str]],
    *,
    needed: bool,
) -> list[str]:
    """A short profile may state each service once; keep a few long phrases."""

    if not needed:
        return []
    fallback: list[str] = []
    for page_index, printed_label, line in lines:
        tokens = _tokens(line)
        run: list[str] = []
        for token in tokens + [""]:
            if token and _is_content(token):
                run.append(token)
                continue
            if len(run) >= 2:
                phrase = _normalize(" ".join(run[:4]))
                if (
                    len(phrase) >= 10
                    and any(len(word) >= 5 for word in run[:4])
                    and phrase not in fallback
                ):
                    fallback.append(phrase)
                    first.setdefault(phrase, (page_index, printed_label, line))
                    counts.setdefault(phrase, 1)
            run = []
        if len(fallback) >= 6:
            break
    return fallback


def harvest_terms(
    pages: list[tuple[int, str | None, str]],
    *,
    strip_phrases: tuple[str, ...] = (),
) -> list[HarvestedTerm]:
    """Read repeated work phrases and their specific words from page text.

    ``pages`` is ``(page_index, printed_label, text)``. The return value is
    ordered phrases first, then words, each with the first page and line it was
    seen on. The function is pure so the rule can be tested without a database.
    """

    lines: list[tuple[int, str | None, str]] = []
    for page_index, printed_label, text in pages:
        for raw in (text or "").splitlines():
            cleaned = _clean_line(raw, strip_phrases=strip_phrases)
            if cleaned:
                lines.append((page_index, printed_label, cleaned))

    counts, first = _phrase_candidates(lines)
    phrases = _select_phrases(counts, first)
    phrases.extend(_fallback_phrases(lines, counts, first, needed=not phrases))

    # Words only become terms through a kept phrase. That is what keeps a
    # generic word ("system") in the document from becoming a suggestion on its
    # own, and it is why a client name, removed above, cannot come back here.
    token_counts: Counter[str] = Counter()
    token_first: dict[str, tuple[int, str | None, str]] = {}
    for phrase in phrases:
        for token in phrase.split():
            if not _is_content(token):
                continue
            if len(token) < 4 and not (token.isupper() and len(token) >= 3):
                continue
            token_counts[token] += counts.get(phrase, 1)
            token_first.setdefault(token, first[phrase])

    tokens = sorted(
        (token for token, count in token_counts.items() if count >= 2),
        key=lambda token: (token_counts[token] * len(token), len(token)),
        reverse=True,
    )
    if len(tokens) < 4:
        # A short profile may name a service once. Keep its long words so the
        # phrase still contributes more than one exact wording.
        extra = sorted(
            (
                token
                for token, count in token_counts.items()
                if count < 2 and len(token) >= 5
            ),
            key=lambda token: (token_counts[token] * len(token), len(token)),
            reverse=True,
        )
        tokens.extend(token for token in extra if token not in tokens)
    tokens = tokens[:MAX_TOKENS]

    result: list[HarvestedTerm] = []
    seen: set[str] = set()
    for term in phrases + tokens:
        normalized = _normalize(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        location = first.get(normalized) or token_first.get(normalized)
        if location is None:
            continue
        page_index, printed_label, line = location
        result.append(
            HarvestedTerm(
                term=normalized,
                page_index=page_index,
                printed_label=printed_label,
                excerpt=line[:MAX_EXCERPT_CHARS],
            )
        )
    return result


class CompanyDocumentIntelService:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        *,
        ai=None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.repository = CompanyDocumentIntelRepository(session)
        self.ai = ai
        self.understanding = CompanyUnderstandingService(
            session, ai=ai, settings=self.settings
        )

    def extract(
        self,
        *,
        document_id: uuid.UUID,
        company_id: uuid.UUID | None = None,
        progress=None,
    ) -> dict:
        document = self.session.get(CompanyDocument, document_id)
        if document is None or document.deleted_at is not None:
            raise NotFound("Document not found.")
        if company_id is not None and document.company_id != company_id:
            raise NotFound("Document not found.")

        if document.document_type != ABOUT_COMPANY_TYPE:
            return self._finish(
                document,
                state="skipped",
                error=(
                    "Only an about-company profile is read for suggestions. "
                    "This document is stored as evidence."
                ),
                terms=None,
                understanding_state="skipped",
                understanding_error=(
                    "Only an about-company profile is read for capabilities."
                ),
            )

        version = self.repository.latest_version(document.id)
        if version is None:
            return self._finish(
                document,
                state="failed",
                error="The document has no stored file.",
                terms=None,
                understanding_state="failed",
                understanding_error="The document has no stored file.",
            )
        if version.mime != "application/pdf":
            return self._finish(
                document,
                state="unsupported",
                error="Upload a PDF company profile so its text can be read.",
                terms=None,
                understanding_state="failed",
                understanding_error=(
                    "Upload a PDF company profile so its text can be read."
                ),
            )

        path = storage.resolve_path(version.storage_path, self.settings)
        if not path.exists():
            return self._finish(
                document,
                state="failed",
                error="The stored file is missing.",
                terms=None,
                understanding_state="failed",
                understanding_error="The stored file is missing.",
            )

        if progress is not None:
            try:
                progress()
            except Exception:  # noqa: BLE001 - a heartbeat is best effort
                pass

        ocr = None
        if self.settings.ocr_enabled:
            try:
                ocr = build_ocr(
                    language=self.settings.ocr_language,
                    tesseract_cmd=self.settings.tesseract_cmd,
                    timeout_seconds=self.settings.ocr_timeout_seconds,
                )
            except OcrUnavailable:
                ocr = None

        try:
            extraction = extract_pdf_pages(
                path.read_bytes(),
                max_pages=self.settings.pdf_max_pages,
                ocr=ocr,
                ocr_dpi=self.settings.ocr_dpi,
                ocr_max_pages=self.settings.ocr_max_pages,
            )
        except PdfExtractionError as exc:
            return self._finish(
                document,
                state="failed",
                error=f"The PDF could not be read: {exc}",
                terms=None,
                understanding_state="failed",
                understanding_error=f"The PDF could not be read: {exc}",
            )

        self.repository.replace_pages(
            version.id,
            [
                {
                    "page_index": page.page_index,
                    "printed_label": page.printed_label,
                    "text": page.text,
                    "char_count": page.char_count,
                    "is_ocr": page.text_source == "ocr",
                }
                for page in extraction.pages
            ],
        )

        company = self.session.get(Company, document.company_id)
        strip_phrases = tuple(
            item
            for item in (getattr(company, "name", None),)
            if item and len(item) >= 4
        )
        harvested = harvest_terms(
            [
                (page.page_index, page.printed_label, page.text)
                for page in extraction.pages
                if page.has_text
            ],
            strip_phrases=strip_phrases,
        )
        self.repository.replace_terms(
            document_id=document.id,
            company_id=document.company_id,
            rows=[
                {
                    "term": item.term,
                    "normalized": item.term.casefold(),
                    "page_index": item.page_index,
                    "printed_label": item.printed_label,
                    "excerpt": item.excerpt,
                    "source": "text_rule",
                    "rules_version": COMPANY_INTEL_VERSION,
                }
                for item in harvested
            ],
        )

        if extraction.text_page_count == 0:
            error = "No text layer was found in this PDF."
            if self.settings.ocr_enabled and ocr is None:
                error = (
                    "No text layer was found and OCR is unavailable. "
                    "Upload a digital copy or install Tesseract."
                )
            elif not self.settings.ocr_enabled:
                error = "No text layer was found and OCR is disabled."
            return self._finish(
                document,
                state="no_text_layer",
                error=error,
                terms=0,
                understanding_state="no_text_layer",
                understanding_error=error,
            )

        understanding = self.understanding.extract(
            document=document,
            pages=[
                {
                    "page_index": page.page_index,
                    "printed_label": page.printed_label,
                    "text": page.text,
                    "has_text": page.has_text,
                    "is_ocr": page.text_source == "ocr",
                }
                for page in extraction.pages
            ],
            company=company,
            progress=progress,
        )

        # The brief is the editable bridge between the PDF reader and fit. An
        # automatic read refreshes a generated brief, but never replaces a
        # human-edited one; the human version stays current until they save a
        # new one, and every old version is still available.
        if understanding.state == "done":
            try:
                from app.services.company_brief import CompanyBriefService

                briefs = CompanyBriefService(self.session)
                current = briefs.current(company.id)
                if current is None or current.source_kind != "human":
                    briefs.generate(
                        company,
                        source_document_id=document.id,
                        source_kind="generated",
                        commit=False,
                    )
            except Exception:  # noqa: BLE001 - a brief is not worth failing a read
                self.session.rollback()

        return self._finish(
            document,
            state="done",
            error=None,
            terms=len(harvested),
            understanding_state=understanding.state,
            understanding_error=understanding.error,
            capability_count=understanding.capabilities,
            experience_count=understanding.experiences,
        )

    def _finish(
        self,
        document: CompanyDocument,
        *,
        state: str,
        error: str | None,
        terms: int | None,
        understanding_state: str | None = None,
        understanding_error: str | None = None,
        capability_count: int | None = None,
        experience_count: int | None = None,
    ) -> dict:
        document.extraction_state = state
        document.extraction_error = error
        # A failed re-read keeps the last good term set and its timestamp: a
        # transient OCR failure must not erase a profile that was read before.
        # Only a completed read (including "no text") replaces both.
        if terms is not None:
            document.term_count = terms
        if state in {"done", "no_text_layer"}:
            document.extracted_at = utcnow()
        if understanding_state is not None:
            document.understanding_state = understanding_state
            document.understanding_error = understanding_error
            if understanding_state in {"done", "no_text_layer"}:
                document.understanding_at = utcnow()
        if capability_count is not None:
            document.capability_count = capability_count
        if experience_count is not None:
            document.experience_count = experience_count
        document.updated_at = utcnow()
        self.session.commit()
        return {
            "document_id": str(document.id),
            "state": state,
            "terms": terms if terms is not None else document.term_count,
            "error": error,
            "understanding_state": document.understanding_state,
            "understanding_error": document.understanding_error,
            "capabilities": document.capability_count,
            "experiences": document.experience_count,
        }

    def terms(self, document: CompanyDocument) -> dict:
        rows = self.repository.terms_for_document(document.id)
        return {
            "items": [
                {
                    "id": row.id,
                    "term": row.term,
                    "kind": "phrase" if " " in row.term else "token",
                    "page_index": row.page_index,
                    "printed_label": row.printed_label,
                    "excerpt": row.excerpt,
                    "source": row.source,
                }
                for row in rows
            ],
            "extraction_state": document.extraction_state,
            "extraction_error": document.extraction_error,
            "extracted_at": document.extracted_at,
            "term_count": len(rows),
        }

    def remove_term(
        self, *, document: CompanyDocument, term_id: uuid.UUID
    ) -> None:
        term = self.repository.get_term(
            document_id=document.id,
            company_id=document.company_id,
            term_id=term_id,
        )
        if term is None:
            raise NotFound("Work term not found.")
        self.repository.delete_term(term)
        document.term_count = self.repository.count_terms(document.id)
        document.updated_at = utcnow()
        self.session.commit()
