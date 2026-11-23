"""Walk a tender portal's directory in a real browser and read the notices.

The portal walk is done in code, not left to an agent's judgement, because a
portal is a fixed sequence of pages and the same sequence should produce the
same tenders every time.

The shape this handles is the one the Indian Central Public Procurement
Portal uses:

1. A directory page listing organisations, with the columns ``S.No``,
   ``Organisation Name`` and ``Tender Count``. The count cell holds a link.
2. Following that link opens the organisation's tender list, with the columns
   ``S.No``, ``Publishing Date``, ``Closing Date``, ``Opening Date``, a single
   ``Title, Ref.No./Tender ID`` column that holds the title, the reference
   number and the link to the notice, and an organisation chain column.
3. The link in the title cell opens the notice itself.

Two rules the portal forces on us:

* A notice cannot be opened by its own address. The address carries a
  session token and the portal answers "your session has expired" when it is
  opened cold, so the whole walk has to happen in one browser session.
* The list paginates, and the pages after the first hold most of the tenders.

The organisation order comes from the company profile alone. A name that states
the work is walked first, then a name in one of the company's regions, then the
rest of the directory in the portal's own order. Nothing is excluded by name:
the row-level gate decides what is listed, and one organisation cannot take the
whole notice budget, so a department reached first cannot hide every later one.
The software carries no vocabulary of authorities or kinds of work of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from app.discovery.browser import BrowserDriver, ChallengeMarker, PageSnapshot
from app.discovery import tender_extraction as extraction
from app.discovery.relevance import company_terms, term_variants, work_type_match
from app.services.job_types import JobCancelled

#: The directory page of the Central Public Procurement Portal.
CPPP_DIRECTORY = (
    "https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation&service=page"
)

#: Script that reads the organisation directory rows.
_ORG_ROWS_JS = """
() => Array.from(document.querySelectorAll('table.list_table tr')).map(tr => {
    const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim());
    const a = tr.querySelector('a[id^=DirectLink]');
    return { cells, name: cells[1] || '', count: cells[2] || '', linkId: a ? a.id : null };
}).filter(r => r.name && r.linkId)
"""

#: Script that reads the tender list rows.
#:
#: The title cell holds two bracketed groups: the first is the title, and it
#: is the link to the notice; the second is the reference number.
_LIST_ROWS_JS = """
() => Array.from(document.querySelectorAll('table.list_table tr')).map(tr => {
    const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim());
    const a = tr.querySelector('td a[href*="FrontEndViewTender"], td a[href*="FrontEndTenderById"]');
    const cell = cells[4] || '';
    const groups = cell.match(/\\[[^\\]]*\\]/g) || [];
    return {
        cells,
        publishing: cells[1] || '',
        closing: cells[2] || '',
        opening: cells[3] || '',
        title: cell,
        chain: cells[5] || '',
        linkText: a ? (a.textContent || '').trim() : null,
        refFromCell: groups.length > 1 ? groups[1].replace(/^\\[|\\]$/g, '').trim() : null,
        href: a ? a.getAttribute('href') : null,
    };
}).filter(r => r.cells.length > 3)
"""


#: Script that reads the document links off a notice page.
#:
#: A tender notice ends with the documents the issuer published: the notice
#: PDF, the tender document, and often a bundle of all of them as a zip. The
#: walk reads them here so the user can copy the address out, and so the
#: workspace can pull the files in later.
#:
#: Only links that clearly name a document are kept. A portal's navigation
#: bar carries the word "document" in places, and a link that does not say
#: what it holds is noise.
_DOCUMENT_LINKS_JS = """
() => {
  const results = [];
  const seen = new Set();
  const FILE = /([^'\"\\s()<>]*\\.(?:zip|pdf|xls|xlsx|doc|docx)(?:\\?[^'\"\\s]*)?)/i;

  const classify = (hay) => {
    const h = hay.toLowerCase();
    if (/\\.zip\\b|\\bzip\\b/.test(h)) return 'zip';
    if (/\\.pdf\\b|\\bpdf\\b/.test(h)) return 'pdf';
    if (/\\.(xls|xlsx|doc|docx)\\b/.test(h)) return 'file';
    return 'unknown';
  };

  const absolute = (value) => {
    try { return new URL(value, document.baseURI).href; }
    catch (e) { return value; }
  };

  const elements = Array.from(document.querySelectorAll(
    'a, button, input[type=button], input[type=submit], [onclick]'
  ));
  for (const el of elements) {
    const rawHref = el.getAttribute('href') || '';
    const label = ((el.textContent || el.value || '') + '').trim().replace(/\\s+/g, ' ').slice(0, 200);
    const onclick = (el.getAttribute('onclick') || '').slice(0, 500);
    const hay = rawHref + ' ' + label + ' ' + onclick;
    const names = /\\bdownload\\b|\\.zip\\b|\\.pdf\\b|\\bzip\\b|\\bpdf\\b|tender\\s*(file|document)|attachment/i.test(hay);
    if (!names) continue;

    let raw = '';
    let url = '';
    // A file named in the address or in the click handler comes first.
    const hit = (rawHref + ' ' + onclick).match(FILE);
    if (hit && hit[1]) {
      raw = hit[1];
      url = absolute(raw);
    }
    // Otherwise the link itself is the download page.
    if (!url && rawHref && !/^(javascript:|#|mailto:|tel:)/i.test(rawHref.trim())) {
      raw = rawHref.trim();
      url = absolute(raw);
    }
    if (!url) continue;

    const key = url + '|' + label;
    if (seen.has(key)) continue;
    seen.add(key);
    results.push({url: url, raw: raw, label: label, kind: classify(hay)});
  }
  return results.slice(0, 40);
}
"""


@dataclass(slots=True)
class PortalRow:
    """One row of a tender list."""

    title: str
    publishing: str
    closing: str
    opening: str
    organisation_chain: str
    href: str
    link_text: str


@dataclass(slots=True)
class OrgWalk:
    """What one organisation's tender list contributed to the run.

    The counts are kept per organisation because the interesting question is
    not "how many rows were seen" but "which department filled the budget".
    """

    name: str
    rows_seen: int = 0
    shortlisted: int = 0
    notices: int = 0
    stopped: str | None = None


@dataclass(slots=True)
class WalkResult:
    """What the walk found."""

    organisation: str | None = None
    organisations_considered: list[str] = field(default_factory=list)
    organisations_seen: int = 0
    rows_seen: int = 0
    pages_walked: int = 0
    shortlisted: int = 0
    notices: list[dict] = field(default_factory=list)
    terminated: int = 0
    organisations: list[OrgWalk] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    challenge: ChallengeMarker | None = None
    stopped_early: str | None = None


#: The CPPP application moved from /nicgep/ to /eprocure/. A tender list can
#: still carry the old path in its links, and the portal answers that path with
#: "Not Found" instead of the notice.
LEGACY_APP_PATH = "/nicgep/app"
LIVE_APP_PATH = "/eprocure/app"

#: What a dead or broken notice page says lives with the other page-reading
#: rules (``tender_extraction.is_error_page``), so the walk and the ingester
#: cannot disagree about whether a page was read.


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _title_key(value: str) -> str:
    """A title without punctuation, for comparing two spellings of one work."""

    return re.sub(r"[^a-z0-9]+", " ", _norm(value or "")).strip()


def _lookup_order(preferred: list[dict], organisations: list[dict]):
    """The chain-head organisations first, then the whole directory once each."""

    seen: set[str] = set()
    for row in list(preferred) + list(organisations):
        key = str(row.get("linkId") or row.get("name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        yield row


def _row_rank(
    row: dict,
    wanted_id: str,
    wanted_ref: str,
    wanted_title: str,
    title: str,
) -> int | None:
    """How strongly a list row matches: 0 id, 1 reference, 2 title.

    The tender id and the reference number are unique identifiers, so they
    are checked before the title; the title is only a fallback for a row whose
    identifiers were not read.
    """

    href = str(row.get("href") or "")
    title_cell = str(row.get("title") or "")
    ref_cell = str(row.get("refFromCell") or "")
    link_text = str(row.get("linkText") or "")
    haystack = _norm(" ".join([href, link_text, title_cell, ref_cell]))
    if wanted_id and wanted_id in haystack:
        return 0
    if wanted_ref and wanted_ref in haystack:
        return 1
    if wanted_title:
        key = _title_key(title)
        if key and (
            key == wanted_title
            or (len(wanted_title) >= 20 and wanted_title in key)
            or (len(key) >= 20 and key in wanted_title)
        ):
            return 2
    return None


def _mentions(text: str, term: str) -> bool:
    """True when the text mentions the term as a word, not inside another word.

    A plain substring match is wrong here. "Broadcast Engineering" contains
    "road", and a directory full of bodies would match on that alone.
    """
    if not text or not term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"[^a-z0-9]+") + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def organisation_tier(name: str, work_terms: list[str]) -> int:
    """How strongly an organisation's own name matches the company's work.

    ``0`` means a multi-word phrase from the profile appears in the name, for
    example "building construction" in "Maharashtra Building Construction
    Corporation"; ``1`` means one profile word appears; ``2`` means the name
    says nothing about the work.

    There is no domain vocabulary here. The profile is the only source of
    work words, so a company that does something this software has never heard
    of is ranked by its own terms. A name that matches nothing is not excluded:
    it is walked after the stronger matches, because the row-level gate decides
    what is actually listed and an unremarkable name may still publish the
    right work.
    """

    if not name:
        return 2
    matched = [term for term in work_terms if _mentions(name, term)]
    if not matched:
        return 2
    # A phrase is a stronger signal than a single word: "road construction"
    # says more about a body than "construction" alone.
    return 0 if any(len(term.split()) > 1 for term in matched) else 1


def _work_terms(company: dict) -> list[str]:
    """Every profile work word, with the same variants the gate matches."""

    terms = company_terms(company)
    words: list[str] = []
    for term in (
        list(terms["core"])
        + list(terms["adjacent"])
        + list(terms["support"])
        + list(terms["generic"])
    ):
        words.extend(_norm(variant) for variant in term_variants(term))
    return words


def match_organisations(rows: list[dict], company: dict) -> list[dict]:
    """Rank every organisation on the directory page, best first.

    The company's own profile decides the order: names that state the work,
    then names in one of the company's regions, then the rest of the directory
    in the order the portal prints it. Nothing is dropped, so a body with an
    uninformative name is still walked once the profile-ranked ones have had
    their turn and the notice budget allows it.
    """

    terms = company_terms(company)
    work_terms = _work_terms(company)
    region_terms = [_norm(term) for term in terms["regions"]]

    candidates: list[tuple[int, bool, dict]] = []
    for row in rows:
        name = _norm(row.get("name"))
        tier = organisation_tier(name, work_terms)
        region_hit = any(_mentions(name, term) for term in region_terms)
        candidates.append((tier, not region_hit, row))

    # Python's sort is stable, so the portal's own order breaks every tie.
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [row for _tier, _region, row in candidates]


def row_matches_profile(title: str, company: dict, *, context: str = "") -> bool:
    """True when the tender title is about the kind of work the company does.

    This is the listing gate's own rule rather than a looser approximation of
    it, so the walk never spends a notice on a row the gate would throw away.
    The organisation chain is passed as context because it names the state,
    and a bare keyword such as "AMC" only counts when the notice is in one of
    the company's regions.
    """
    if not _norm(title):
        return False
    return work_type_match(company, title=title, snippet=title, context=context)


def closing_is_open(closing: str, *, today: date | None = None) -> bool:
    """True when the closing date the list shows is still in the future."""
    if not closing:
        return False
    facts = extraction.read_close_date(closing, today=today)
    if not facts.deadline:
        return False
    return facts.deadline >= (today or date.today())


class PortalWalker:
    """Walks a portal's directory in one browser session."""

    def __init__(
        self,
        driver: BrowserDriver,
        company: dict,
        *,
        directory_url: str = CPPP_DIRECTORY,
        max_pages: int = 5,
        max_notices: int = 10,
        max_notices_per_org: int = 4,
        max_org_pages: int = 4,
        today: date | None = None,
        on_step: Callable[[], None] | None = None,
    ) -> None:
        self.driver = driver
        self.company = company
        self.directory_url = directory_url
        self.max_pages = max_pages
        self.max_notices = max_notices
        self.max_notices_per_org = max_notices_per_org
        self.max_org_pages = max_org_pages
        self.today = today or date.today()
        self.on_step = on_step
        #: Notices whose page never loaded (a dead link, an error page). They
        #: are counted so a run can say it could not read them.
        self.unreadable = 0
        self.terminated = 0
        self._seen_sources: set[str] = set()

    def _source_candidate(self, result: "WalkResult", url: str | None, *, reason: str) -> None:
        if not url or url in self._seen_sources:
            return
        self._seen_sources.add(url)
        result.candidates.append(
            {
                "url": url,
                "kind": "source",
                "outcome": "stored",
                "reason": reason,
                "listing_url": None,
            }
        )

    def _step(self) -> None:
        if self.on_step is not None:
            try:
                self.on_step()
            except JobCancelled:
                # A cancelled walk must stop now, not after the next notice.
                raise
            except Exception:  # noqa: BLE001 - a heartbeat must not stop the walk
                pass

    def walk(self) -> WalkResult:
        result = WalkResult()
        page = getattr(self.driver, "_page", None)
        if page is None:
            result.stopped_early = "The browser session has no page."
            return result
        self._step()

        self.driver.open(self.directory_url)
        self._source_candidate(result, self.directory_url, reason="Directory page was read.")

        # The portal puts a captcha on its search form, but the directory
        # table below it is already readable. A captcha only blocks the walk
        # when there is no directory to read: stopping on every page that
        # carries the search form would make the portal unusable.
        organisations = list(page.evaluate(_ORG_ROWS_JS))
        if not organisations:
            marker = self.driver.find_challenge()
            if marker is not None:
                result.challenge = marker
                result.stopped_early = (
                    "The portal asked for a human before the list was read."
                )
                return result

        # The directory is alphabetical and paginated, so the organisation we
        # want may be several pages in. The pages are read here, before any
        # organisation is opened, so the whole directory is searched.
        for _page_number in range(2, self.max_org_pages + 1):
            if not self._go_next_page(page):
                break
            more = page.evaluate(_ORG_ROWS_JS)
            if not more:
                break
            organisations.extend(more)

        chosen = match_organisations(organisations, self.company)
        # "Considered" is the profile-matched shortlist, not the whole
        # directory: the run report reads as "these matched your profile and
        # were walked first", while organisations_seen counts the directory.
        work_terms = _work_terms(self.company)
        result.organisations_considered = [
            row["name"]
            for row in organisations
            if organisation_tier(_norm(row.get("name")), work_terms) < 2
        ][:50]
        result.organisations_seen = len(organisations)
        if not chosen:
            result.stopped_early = "The directory page listed no organisations."
            return result

        for organisation in chosen:
            if len(result.notices) >= self.max_notices:
                break
            self._step()
            record = OrgWalk(name=organisation["name"])
            result.organisations.append(record)
            remaining = self.max_notices - len(result.notices)
            limit = remaining
            if self.max_notices_per_org > 0:
                limit = min(self.max_notices_per_org, remaining)

            try:
                page.click(f"#{organisation['linkId']}")
                page.wait_for_timeout(4000)
            except Exception as exc:  # noqa: BLE001 - a dead link must not stop the walk
                record.stopped = "could_not_open"
                result.stopped_early = f"Could not open {organisation['name']}: {exc}"
                continue

            # The click has to have landed. When it has not, something is
            # blocking the page, and the only thing that can clear it is a
            # person.
            if not self._landed_on_a_list():
                marker = self.driver.find_challenge()
                if marker is not None:
                    record.stopped = "challenge"
                    result.challenge = marker
                    result.stopped_early = "The portal asked for a human."
                    return result
                record.stopped = "not_a_tender_list"
                result.stopped_early = (
                    f"The link to {organisation['name']} did not open a tender list."
                )
                self._back_to_directory()
                continue

            # A captcha on the list page belongs to the portal's search form,
            # not to the list: the list is already on screen, so it is read.
            # A real block would have failed the landing test above.

            if not result.organisation:
                result.organisation = organisation["name"]
            self._walk_list(page, organisation["name"], result, limit, record)
            if len(result.notices) >= self.max_notices:
                break
            # Go back to the directory for the next organisation. One body
            # cannot take the whole budget: a department's list holds every
            # kind of work it does, not just the work we came for.
            self._back_to_directory()
        return result

    def open_tender(
        self,
        *,
        portal_tender_id: str | None,
        reference: str | None,
        organisation_chain: str | None,
        work_title: str | None,
    ) -> dict | None:
        """Find one known tender again and open its notice.

        The notice address carries a session token and is useless days later.
        The stable locator is the portal tender id, so the walk goes back to
        the directory, opens the organisation named in the locator, and finds
        the row by tender id (or by reference and title when the portal has
        changed the id). It is the same fixed path a run takes, not a guess.
        """

        page = getattr(self.driver, "_page", None)
        if page is None:
            return None
        wanted_id = _norm(portal_tender_id)
        wanted_ref = _norm(reference)
        wanted_title = _norm(work_title)
        chain_head = (organisation_chain or "").split("||")[0].strip()

        organisations = self._directory_rows(page)
        if organisations is None:
            return None

        preferred = [
            row
            for row in organisations
            if chain_head
            and _norm(row.get("name"))
            and (
                _norm(chain_head) in _norm(row.get("name"))
                or _norm(row.get("name")) in _norm(chain_head)
            )
        ]
        # The organisation named in the locator is looked at first. The
        # complete directory is the final fallback: the chain head may name a
        # page differently from the directory, and the tender id or reference
        # is unique wherever the row sits.
        for organisation in _lookup_order(preferred, organisations):
            match = self._find_row(
                page,
                organisation,
                wanted_id=wanted_id,
                wanted_ref=wanted_ref,
                wanted_title=wanted_title,
            )
            if match is None:
                continue
            try:
                listing_url = getattr(page, "url", None)
                page.goto(
                    self._notice_address(str(match["href"])),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(2500)
                return self._notice_from_open_page(
                    page,
                    match,
                    str(organisation.get("name") or ""),
                    listing_url,
                )
            except Exception:  # noqa: BLE001 - try the next organisation
                continue
        return None

    #: A lookup may scan further than a discovery walk, because it is looking
    #: for one known row and stopping at it. The bounds keep a broken portal
    #: from holding the job open forever.
    LOOKUP_MAX_ORG_PAGES = 40
    LOOKUP_MAX_LIST_PAGES = 60

    def _directory_rows(self, page) -> list[dict] | None:
        """Open the directory and read every organisation row it offers."""

        try:
            self.driver.open(self.directory_url)
        except Exception:  # noqa: BLE001
            return None
        organisations = list(page.evaluate(_ORG_ROWS_JS))
        if not organisations and self.driver.find_challenge() is not None:
            return None
        for _page_number in range(2, self.LOOKUP_MAX_ORG_PAGES + 1):
            if not self._go_next_page(page):
                break
            more = page.evaluate(_ORG_ROWS_JS)
            if not more:
                break
            organisations.extend(more)
        return organisations

    def _find_row(
        self,
        page,
        organisation: dict,
        *,
        wanted_id: str,
        wanted_ref: str,
        wanted_title: str,
    ) -> dict | None:
        """Scan one organisation's list and return the best matching row.

        The precedence is deliberate: the tender id and the reference number
        are unique, so they win over a title, and the title is only used when
        both are missing from the row. The scan stops as soon as the unique
        id is found.
        """

        try:
            page.click(f"#{organisation['linkId']}")
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            return None
        if not self._landed_on_a_list():
            self._back_to_directory()
            return None

        best: tuple[int, dict] | None = None
        for _page_number in range(1, self.LOOKUP_MAX_LIST_PAGES + 1):
            rows = page.evaluate(_LIST_ROWS_JS)
            for row in rows:
                rank = _row_rank(
                    row,
                    wanted_id,
                    wanted_ref,
                    wanted_title,
                    title=self._clean_title(
                        row.get("linkText") or row.get("title") or ""
                    ),
                )
                if rank is None:
                    continue
                if best is None or rank < best[0]:
                    best = (rank, row)
                if rank == 0:
                    break
            if best is not None and best[0] == 0:
                break
            if not self._go_next_page(page):
                break
        if best is None:
            self._back_to_directory()
            return None
        return best[1]

    def _landed_on_a_list(self) -> bool:
        """True when the page now shows a tender list rather than the directory.

        The list is recognised by its columns, not by the address, because the
        portal keeps the session in the address.
        """
        page = getattr(self.driver, "_page", None)
        if page is None:
            return False
        try:
            rows = page.evaluate(_LIST_ROWS_JS)
        except Exception:  # noqa: BLE001
            return False
        return any(row.get("closing") for row in rows)

    def _back_to_directory(self) -> None:
        try:
            self.driver.open(self.directory_url)
        except Exception:  # noqa: BLE001
            pass

    def _walk_list(
        self,
        page,
        organisation: str,
        result: WalkResult,
        limit: int,
        record: OrgWalk,
    ) -> None:
        """Read one organisation's tender list, up to ``limit`` notices."""
        taken = 0
        for _page_number in range(1, self.max_pages + 1):
            if taken >= limit:
                record.stopped = "organisation_quota"
                return
            rows = page.evaluate(_LIST_ROWS_JS)
            result.pages_walked += 1
            result.rows_seen += len(rows)
            record.rows_seen += len(rows)
            listing_url = getattr(page, "url", None)
            self._source_candidate(
                result, listing_url, reason=f"Tender list for {organisation} was read."
            )

            for row in rows:
                if taken >= limit:
                    record.stopped = "organisation_quota"
                    return
                self._step()
                # The link text is the tender's name; the cell also carries
                # the reference number in a second bracketed group.
                title = self._clean_title(row.get("linkText") or row.get("title") or "")
                href = row.get("href")
                if not href:
                    # A row with no link cannot be opened, so it cannot be
                    # cited. It is not a stored tender and it is not counted
                    # as read either.
                    continue
                if not row_matches_profile(
                    title, self.company, context=row.get("chain") or ""
                ):
                    result.candidates.append(
                        {
                            "url": href,
                            "kind": "tender_candidate",
                            "outcome": "rejected",
                            "reason": "The work did not match the target profile.",
                            "listing_url": listing_url,
                            "title": title,
                            "organisation": organisation,
                            "reference_number": row.get("refFromCell"),
                        }
                    )
                    continue
                if not closing_is_open(row.get("closing") or "", today=self.today):
                    result.candidates.append(
                        {
                            "url": href,
                            "kind": "tender_candidate",
                            "outcome": "rejected",
                            "reason": "The closing date was missing or in the past.",
                            "listing_url": listing_url,
                            "title": title,
                            "organisation": organisation,
                            "reference_number": row.get("refFromCell"),
                        }
                    )
                    continue
                result.shortlisted += 1
                record.shortlisted += 1
                notice = self._read_notice(page, row, organisation)
                if notice:
                    if notice.get("cycle_state") == "terminated":
                        # The portal says this cycle is over. It is not a
                        # tender: it is not listed, not stored and not counted
                        # against the notice budget.
                        result.terminated += 1
                        result.candidates.append(
                            {
                                "url": notice.get("notice_url") or href,
                                "kind": "tender_candidate",
                                "outcome": "rejected",
                                "reason": (
                                    "The portal states this bidding cycle is "
                                    "terminated."
                                ),
                                "listing_url": listing_url,
                                "title": title,
                                "organisation": organisation,
                                "tender_id": notice.get("tender_id"),
                                "reference_number": notice.get("reference_number")
                                or row.get("refFromCell"),
                            }
                        )
                        continue
                    if notice.get("reference_number") is None and row.get("refFromCell"):
                        notice["reference_number"] = row["refFromCell"]
                    result.notices.append(notice)
                    result.candidates.append(
                        {
                            "url": notice.get("notice_url") or href,
                            "kind": "tender_candidate",
                            "outcome": "stored",
                            "reason": None,
                            "listing_url": listing_url,
                        }
                    )
                    record.notices += 1
                    taken += 1
                else:
                    result.candidates.append(
                        {
                            "url": href,
                            "kind": "tender_candidate",
                            "outcome": "unreadable",
                            "reason": "The notice page could not be read.",
                            "listing_url": listing_url,
                            "title": title,
                            "organisation": organisation,
                            "reference_number": row.get("refFromCell"),
                        }
                    )

            if not self._go_next_page(page):
                return

    def _return_to_list(self, page) -> None:
        """Come back to the tender list a notice was opened from.

        The walk continues from the list, so the notice page has to be left
        behind. When the browser will not go back, the next read simply finds
        no rows and the walk ends, which is better than a crash.
        """
        try:
            page.go_back()
            page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001 - a missing history entry must not stop the walk
            pass

    def _go_next_page(self, page) -> bool:
        """Move to the next page of the list, when there is one."""
        try:
            clicked = page.evaluate(
                """() => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const next = links.find(a => {
                        const t = (a.textContent || '').trim().toLowerCase();
                        return t === 'next' || t === '>' || t === 'next page';
                    });
                    if (!next) return false;
                    next.click();
                    return true;
                }"""
            )
        except Exception:  # noqa: BLE001
            return False
        if not clicked:
            return False
        page.wait_for_timeout(3000)
        return True

    def _read_notice(self, page, row: dict, organisation: str) -> dict | None:
        """Open one notice, read it, and come back to the list.

        The notice page is left behind in a ``finally``: the walk continues
        from the list, so a notice that is read must not leave the next row
        unreachable.
        """
        href = row["href"]
        url = self._notice_address(href)
        listing_url = getattr(page, "url", None)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            self.unreadable += 1
            self._return_to_list(page)
            return None

        try:
            return self._notice_from_open_page(page, row, organisation, listing_url)
        finally:
            self._return_to_list(page)

    def _notice_from_open_page(
        self,
        page,
        row: dict,
        organisation: str,
        listing_url: str | None,
    ) -> dict | None:
        """Read one notice from the page that is already open on it."""

        snapshot: PageSnapshot = self.driver.snapshot()
        text = snapshot.text or ""
        if "session in the client area has expired" in text.lower():
            self.unreadable += 1
            return None
        if self._is_error_page(text):
            # A stale /nicgep/ link answers "Not Found". Nothing was read,
            # so nothing may be claimed from it.
            self.unreadable += 1
            return None

        # A page can be a notice of a different kind: the portal saying the
        # bidding cycle is over. That is not a tender with changed fields, and
        # its old details must not be presented as if it were still biddable.
        termination = extraction.termination_statement(text)
        if termination is not None:
            self.terminated += 1
            return {
                "cycle_state": "terminated",
                "termination_excerpt": termination,
                "title": self._clean_title(
                    row.get("linkText") or row.get("title") or ""
                )[:400],
                "notice_url": snapshot.url,
                "listing_url": listing_url,
                "page_text": text,
                "authority": organisation,
                "organisation_chain": extraction.normalize_text(
                    row.get("chain") or self._labelled(text, "Organisation Chain")
                ),
                "tender_id": self._labelled(text, "Tender ID"),
                "reference_number": (
                    self._labelled(text, "Tender Reference Number")
                    or row.get("refFromCell")
                ),
                "snippet": self._clean_title(
                    row.get("linkText") or row.get("title") or ""
                )[:500],
                "verified": True,
            }

        # The title is the tender's name, never the reference number that
        # sits beside it in the same list cell.
        title = self._clean_title(row.get("linkText") or row.get("title") or "")
        extracted = extraction.extract_fields(title, "", text)
        # The documents the issuer published with the notice: the notice
        # PDF, the tender document, and often a zip of all of them.
        links = self._document_links(page)
        # The notice states its own values on labelled lines. Those are
        # preferred over the generic extractor, which can pick up the
        # site's own navigation text.
        tender_id = self._labelled(text, "Tender ID")
        reference = (
            self._labelled(text, "Tender Reference Number")
            or extracted.reference_number
        )
        location = self._labelled(text, "Location") or extracted.location
        value = self._notice_value(text) or extracted.value_raw
        bid_validity = self._labelled(text, "Bid Validity(Days)")
        published = self._labelled(text, "Published Date")
        opening = self._labelled(text, "Bid Opening Date")
        closing = (
            self._labelled(text, "Bid Submission End Date")
            or row.get("closing")
            or extracted.deadline
        )
        return {
            "title": title[:400],
            "notice_url": snapshot.url,
            "listing_url": listing_url,
            "page_text": text,
            "authority": organisation,
            "organisation_chain": extraction.normalize_text(
                row.get("chain") or self._labelled(text, "Organisation Chain")
            ),
            "tender_id": tender_id,
            "reference_number": reference,
            "closing_date": closing,
            "publishing_date": published or row.get("publishing"),
            "opening_date": opening or row.get("opening"),
            "location": location,
            "value": value,
            "bid_validity": bid_validity,
            "snippet": title[:500],
            "document_links": links,
            "extracted_evidence": extracted.evidence,
            # These were read from labelled fields on the notice itself,
            # so they are precise in a way a generic scan is not.
            "verified": True,
        }

    @staticmethod
    def _document_links(page) -> list[dict]:
        """Read the document links off the notice page.

        A missing link list is not an error: a notice with no published
        document simply has none, and the walk still read the notice.
        """
        try:
            found = page.evaluate(_DOCUMENT_LINKS_JS)
        except Exception:  # noqa: BLE001 - a page without links is normal
            return []
        if not isinstance(found, list):
            return []
        links: list[dict] = []
        for item in found:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            kind = str(item.get("kind") or "unknown")
            links.append(
                {
                    "url": url[:2000],
                    "label": str(item.get("label") or "")[:200],
                    "kind": kind if kind in {"zip", "pdf", "file", "unknown"} else "unknown",
                    "raw": str(item.get("raw") or "")[:2000] or None,
                }
            )
        return links

    @staticmethod
    def _notice_value(text: str) -> str | None:
        """Read the tender value off the line that states it.

        The portal writes ``Tender Value in Rs 3,38,81,600`` with the amount
        and the currency marker on the same line, so the first number on that
        line is the value.
        """
        for line in text.splitlines():
            if "tender value" not in line.casefold():
                continue
            numbers = re.findall(r"[\d][\d,]*(?:\.\d+)?", line)
            if numbers:
                return numbers[0]
        return None

    @staticmethod
    def _notice_address(href: str) -> str:
        """The address to open a notice at.

        The list links are relative and some still point at the old
        ``/nicgep/`` application, which the portal answers with "Not Found".
        The same application is served at ``/eprocure/``, so the path is
        corrected before the page is opened.
        """
        url = href if href.startswith("http") else f"https://etenders.gov.in{href}"
        return url.replace(LEGACY_APP_PATH, LIVE_APP_PATH)

    @staticmethod
    def _is_error_page(text: str) -> bool:
        """True when the page is an error page rather than a notice.

        A notice is a long, structured page. The portal's failure pages are a
        sentence long, so the check is deliberately narrow: a long page is
        never thrown away for containing a word.
        """
        return extraction.is_error_page(text)

    @staticmethod
    def _clean_title(raw: str) -> str:
        """Read the tender's name out of a list cell.

        The list writes the name as ``[Tender Name]`` and puts the reference
        number in a second bracketed group, so the cell reads
        ``[Name] [Ref.No]``. Only the first group is the name. Stripping the
        outermost pair from a cell that holds two leaves ``Name] [Ref.No``,
        which is neither a title nor a reference number.
        """
        title = extraction.normalize_text(raw)
        if not title:
            return title
        leading = re.match(r"^\[([^\]]*)\]\s*(.*)$", title)
        if leading and leading.group(1).strip():
            return leading.group(1).strip()
        # A cell can arrive cut short, with the reference number and the tender
        # id still hanging off the end: "Name.] [Ref][TenderId". The name ends
        # where the next bracketed group begins.
        cut = re.split(r"\]\s*\[|\]\[", title)[0]
        if cut != title:
            title = cut.rstrip("]").strip() or title
        # A name that is not wrapped, with the reference number still hanging
        # off the end of the cell.
        return re.sub(r"\s*\[[^\]]*\]\s*$", "", title).strip() or title

    @staticmethod
    def _labelled(text: str, label: str) -> str | None:
        """Read a value the notice states next to a label.

        The notice lays its values out as ``Label<TAB>value``, sometimes
        several pairs on one line, so the value is taken up to the next tab
        or the end of the line.
        """
        pattern = re.compile(
            rf"(?:^|\n|\t)[ \t]*{re.escape(label)}[ \t]*\t?[ \t]*([^\t\n\r]*)",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if not match:
            return None
        value = extraction.normalize_text(match.group(1))
        if not value:
            return None
        # A value that is really the next label means the field was empty.
        if value.lower() in {"na", "n/a", "-"}:
            return None
        return value

