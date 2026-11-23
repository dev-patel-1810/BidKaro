"""The nightly listing sweep.

A sweep is deliberately *not* a discovery walk. The walk answers "which of
today's notices might suit this company", so it opens notices, compares work
types against the profile and stops when it has enough. A sweep answers "what
is on the portal at all", so it reads the directory and every listing page and
stops before the notice page: a listing row already carries the work, the
reference, the portal tender id and the dates, which is everything the corpus
needs to be useful and everything the read-time matcher needs to decide
relevance later.

Two consequences matter:

* No profile is consulted. The corpus is global and per-company relevance is
  computed when the list is read, so a sweep that filtered by profile would
  bake today's profile into the corpus and quietly lose everything the company
  did not declare yet.
* Progress is per organisation page, not per fixed batch of rows. The caller
  receives one batch per page, stores it, and commits before asking for the
  next one. Cancelling therefore keeps every page that was already read and
  costs at most the page in flight.

The portal binds a notice address to the browser session that opened it, so
the durable identifier in every stored row is the portal's own tender id, not
the address. The address is kept as evidence in the observation trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.discovery import tender_extraction as extraction
from app.discovery.portal_walk import _LIST_ROWS_JS, _ORG_ROWS_JS, PortalWalker


def organisation_ages(session) -> dict[str, datetime]:
    """When each organisation was last read, for staleness ordering.

    Read from the observation trail, not from a saved cursor: the trail is
    already the record of what was read and when, so a cursor would be a second
    copy of the same fact that can disagree with it.
    """

    from sqlalchemy import func, select

    from app.models.intelligence import TenderObservation

    rows = session.execute(
        select(
            TenderObservation.organisation,
            func.max(TenderObservation.observed_at),
        )
        .where(TenderObservation.organisation.is_not(None))
        .group_by(TenderObservation.organisation)
    ).all()
    ages: dict[str, datetime] = {}
    for name, seen in rows:
        if name and seen is not None:
            ages[str(name)] = seen
    return ages


@dataclass(slots=True)
class SweepPage:
    """One tender list page, already turned into listing rows."""

    organisation: str
    listing_url: str | None
    page_number: int
    rows_seen: int
    notices: list[dict[str, Any]]


@dataclass(slots=True)
class SweepResult:
    pages: int = 0
    rows_seen: int = 0
    organisations_total: int = 0
    organisations_walked: int = 0
    stopped_early: str | None = None
    challenge: Any | None = None
    errors: list[str] = field(default_factory=list)


#: Called once per list page. It must store what it is given and return. The
#: sweep does not keep the rows, so a page that was stored is never lost.
PageSink = Callable[[SweepPage], None]


class PortalSweeper(PortalWalker):
    """Read the whole directory and every list page, opening no notice."""

    def __init__(
        self,
        driver,
        *,
        organisation_age: dict[str, datetime] | None = None,
        max_organisations: int = 0,
        max_pages_per_org: int = 0,
        **kwargs: Any,
    ) -> None:
        # The parent holds the browser, the directory address, the paging
        # helpers and the step hook. A sweep has no profile, so it passes an
        # empty one: nothing in the listing path consults it.
        super().__init__(driver, {}, **kwargs)
        self.organisation_age = organisation_age or {}
        self.max_organisations = max_organisations
        self.max_pages_per_org = max_pages_per_org

    # -- public ------------------------------------------------------------

    def sweep(self, on_page: PageSink) -> SweepResult:
        result = SweepResult()
        page = getattr(self.driver, "_page", None)
        if page is None:
            result.stopped_early = "The browser session has no page."
            return result
        self._step()

        self.driver.open(self.directory_url)
        organisations = list(page.evaluate(_ORG_ROWS_JS))
        if not organisations:
            marker = self.driver.find_challenge()
            if marker is not None:
                result.challenge = marker
                result.stopped_early = (
                    "The portal asked for a human before the directory was read."
                )
                return result
            result.stopped_early = "The directory page listed no organisations."
            return result

        # The directory is paginated. Every page is read before any
        # organisation is opened, so the sweep sees the whole directory rather
        # than the first page of it.
        for _page_number in range(2, self.max_org_pages + 1):
            if not self._go_next_page(page):
                break
            more = page.evaluate(_ORG_ROWS_JS)
            if not more:
                break
            organisations.extend(more)

        ordered = self._order(organisations)
        result.organisations_total = len(ordered)
        if self.max_organisations:
            ordered = ordered[: self.max_organisations]

        for organisation in ordered:
            # Cancellation is checked between organisations as well as between
            # rows: a sweep that has been told to stop does not open a new
            # organisation first.
            self._step()
            name = str(organisation.get("name") or "")
            try:
                page.click(f"#{organisation['linkId']}")
                page.wait_for_timeout(4000)
            except Exception as exc:  # noqa: BLE001 - a dead link must not stop the sweep
                result.errors.append(
                    f"Could not open {name}: {type(exc).__name__}"
                )
                self._back_to_directory()
                continue

            if not self._landed_on_a_list():
                marker = self.driver.find_challenge()
                if marker is not None:
                    result.challenge = marker
                    result.stopped_early = (
                        "The portal asked for a human while a tender list was read."
                    )
                    return result
                result.errors.append(f"{name} did not open a tender list.")
                self._back_to_directory()
                continue

            result.organisations_walked += 1
            self._read_pages(page, name, result, on_page)
            self._back_to_directory()

        return result

    # -- internals ---------------------------------------------------------

    def _order(self, organisations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Staleness order, then the directory's own order.

        An organisation that was never read comes first, then the one that was
        read longest ago. That makes a cancelled sweep self-healing: the next
        run starts where the last one stopped instead of re-reading the top of
        the directory forever, and nothing needs a saved cursor.
        """

        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in organisations:
            name = str(row.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            unique.append(row)

        epoch = datetime.min.replace(tzinfo=timezone.utc)

        def age(row: dict[str, Any]) -> datetime:
            value = self.organisation_age.get(str(row.get("name") or "").strip())
            if value is None:
                return epoch
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value

        indexed = list(enumerate(unique))
        indexed.sort(key=lambda pair: (age(pair[1]), pair[0]))
        return [row for _index, row in indexed]

    def _read_pages(
        self,
        page,
        organisation: str,
        result: SweepResult,
        on_page: PageSink,
    ) -> None:
        page_number = 1
        while True:
            rows = page.evaluate(_LIST_ROWS_JS)
            listing_url = getattr(page, "url", None)
            notices = self._listing_rows(
                rows, organisation, listing_url, page_number
            )
            result.pages += 1
            result.rows_seen += len(rows)
            on_page(
                SweepPage(
                    organisation=organisation,
                    listing_url=listing_url,
                    page_number=page_number,
                    rows_seen=len(rows),
                    notices=notices,
                )
            )
            if self.max_pages_per_org and page_number >= self.max_pages_per_org:
                return
            if not self._go_next_page(page):
                return
            page_number += 1

    def _listing_rows(
        self,
        rows: list[dict[str, Any]],
        organisation: str,
        listing_url: str | None,
        page_number: int,
    ) -> list[dict[str, Any]]:
        """Turn one list page into notice-shaped dictionaries.

        The fields come from the row the portal printed, so nothing is
        claimed that the row does not say. ``page_text`` stays empty on
        purpose: no notice page was opened, and a caller that wants to cite
        one must open it.
        """

        notices: list[dict[str, Any]] = []
        for position, row in enumerate(rows, start=1):
            cell = str(row.get("title") or "")
            title, reference, tender_id = extraction.split_list_cell(cell)
            title = self._clean_title(str(row.get("linkText") or title or ""))
            if not title or extraction.is_listing_page(title):
                continue
            href = row.get("href")
            notices.append(
                {
                    "title": title[:400],
                    "tender_id": tender_id,
                    "reference_number": reference or row.get("refFromCell"),
                    "closing_date": row.get("closing") or "",
                    "publishing_date": row.get("publishing") or "",
                    "opening_date": row.get("opening") or "",
                    "authority": organisation,
                    "organisation_chain": extraction.normalize_text(
                        str(row.get("chain") or "")
                    ),
                    "listing_url": listing_url,
                    "notice_url": (
                        self._notice_address(str(href)) if href else None
                    ),
                    "page_text": "",
                    "verified": False,
                    "sweep": {
                        "list_page": page_number,
                        "row_position": position,
                    },
                }
            )
        return notices
