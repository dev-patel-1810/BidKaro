from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.time import utcnow
from app.models.assessment import CompanyTender
from app.models.corpus import (
    CompanyRequirementFlag,
    DocumentFetchState,
    DocumentPage,
    Requirement,
    Tender,
    TenderCover,
    TenderDocument,
    TenderLocator,
)

#: The orders a tender list can be shown in. The default is arrival order:
#: a tender that entered the corpus most recently is first. Closing-date order
#: is a deliberate choice by the reader, not the default, because a tender read
#: today must not be buried under an older one whose deadline happens to be
#: sooner. Every order ends with the id so two rows written in the same tick
#: still have a stable page position.
TENDER_ORDERS = {
    "created_desc": (Tender.created_at.desc(), Tender.id.desc()),
    "created_asc": (Tender.created_at.asc(), Tender.id.asc()),
    "closing_asc": (Tender.closing_at.asc().nulls_last(), Tender.id.asc()),
    "closing_desc": (Tender.closing_at.desc().nulls_last(), Tender.id.desc()),
}
DEFAULT_TENDER_ORDER = "created_desc"

#: Best fit first, newest first within the same fit. The fit is the company's
#: read-time verdict, which is not a column, so the repository has no SQL order
#: for it (it falls back to newest) and the service sorts the visible rows
#: after the gate. Asking for it by name keeps one validated vocabulary of
#: sorts rather than two lists that drift.
MATCH_ORDER = "match_desc"
TENDER_SORTS = frozenset(TENDER_ORDERS) | {MATCH_ORDER}


def like_pattern(value: str) -> str:
    """A contains-pattern that treats % and _ in the user's words literally."""

    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


#: How much a read can tell us, best first. A manual upload is the user's own
#: document, a notice page is the tender's own page, a search result is a
#: description, and a listing row is only a line in a table. A later, weaker
#: read must never replace the provenance of a stronger one.
_PROVENANCE_RANK = {
    "manual_upload": 3,
    "notice_page": 2,
    "search_result": 1,
    "listing_table": 0,
}


def _provenance_rank(value: str | None) -> int:
    return _PROVENANCE_RANK.get(str(value or ""), 0)


class TenderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, tender_id: uuid.UUID, *, include_merged: bool = False) -> Tender | None:
        statement = select(Tender).where(
            Tender.id == tender_id, Tender.deleted_at.is_(None)
        )
        if not include_merged:
            statement = statement.where(Tender.merged_into_tender_id.is_(None))
        return self.session.scalar(statement)

    def find_by_dedupe_key(self, dedupe_key: str) -> Tender | None:
        return self.session.scalar(
            select(Tender).where(
                Tender.dedupe_key == dedupe_key, Tender.deleted_at.is_(None)
            )
        )

    def find_by_portal_identity(
        self, portal_id: uuid.UUID, portal_tender_id: str
    ) -> Tender | None:
        return self.session.scalar(
            select(Tender).where(
                Tender.portal_id == portal_id,
                Tender.portal_tender_id == portal_tender_id,
                Tender.deleted_at.is_(None),
            )
        )

    def upsert(
        self, values: dict[str, Any], *, dedupe_key: str
    ) -> tuple[Tender, bool]:
        """Insert or carry a read forward, never emptying an earlier field.

        Identity is the portal's own tender id when both the portal and the id
        are known; otherwise it is the deterministic dedupe key. A second read
        fills gaps and replaces a stated value, but a missing value in the new
        read never overwrites a value the first read had.
        """

        existing = None
        portal_id = values.get("portal_id")
        portal_tender_id = values.get("portal_tender_id")
        if portal_id is not None and portal_tender_id:
            existing = self.find_by_portal_identity(portal_id, portal_tender_id)
        if existing is None:
            existing = self.find_by_dedupe_key(dedupe_key)

        if existing is None:
            payload = dict(values)
            payload["dedupe_key"] = dedupe_key
            return self.create(payload), True

        for field, new_value in values.items():
            if field in {"id", "dedupe_key", "created_at"}:
                continue
            if field == "raw" and new_value:
                merged = dict(existing.raw or {})
                merged.update(new_value)
                existing.raw = merged
                continue
            if new_value is None:
                continue
            if field == "extracted_from" and _provenance_rank(
                new_value
            ) < _provenance_rank(existing.extracted_from):
                # A listing row read later must not downgrade a tender that was
                # already read from its own notice page: the fuller read is the
                # better citation and the listing row cannot replace it.
                continue
            setattr(existing, field, new_value)
        existing.updated_at = utcnow()
        self.session.flush()
        return existing, False

    def list(
        self,
        *,
        closing_after: datetime | None = None,
        sort: str = DEFAULT_TENDER_ORDER,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[Tender]:
        statement = (
            select(Tender)
            .where(Tender.deleted_at.is_(None), Tender.merged_into_tender_id.is_(None))
            .order_by(*TENDER_ORDERS.get(sort, TENDER_ORDERS[DEFAULT_TENDER_ORDER]))
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        if closing_after is not None:
            statement = statement.where(
                or_(
                    Tender.closing_at.is_(None),
                    Tender.closing_at >= closing_after,
                )
            )
        return list(self.session.scalars(statement).all())

    def list_for_company(
        self,
        company_id: uuid.UUID,
        *,
        closing_after: datetime | None = None,
        saved: bool = False,
        bidding: bool = False,
        mine: bool = False,
        include_dismissed: bool = False,
        query: str | None = None,
        sort: str = DEFAULT_TENDER_ORDER,
        limit: int | None = None,
        offset: int = 0,
        match_patterns: list[str] | None = None,
    ) -> list[tuple[Tender, CompanyTender | None]]:
        """The global corpus plus this company's saved/dismissed judgement.

        Relevance is decided in the service, because it is profile-dependent
        and therefore cannot be a storage filter. This query only applies the
        hard filters - closing date, saved/bidding/mine, dismissed, the search
        words - and the requested order. ``limit`` is only a safety valve: a
        page cannot be sliced here because the profile gate runs afterwards,
        so the service filters first and slices the visible rows.

        ``match_patterns`` is a *superset* narrowing, not the gate. Every
        pattern is a ``contains`` fragment of one declared profile term, and
        the query keeps a row when its work words contain any of them, or when
        it is a manual upload or one the company saved or is bidding on. The
        service still runs the real gate over what comes back; the point is
        only that a row with none of the company's words in its work fields
        can never pass that gate, so it need not be loaded or scanned in
        Python.
        """

        statement = (
            select(Tender, CompanyTender)
            .outerjoin(
                CompanyTender,
                and_(
                    CompanyTender.tender_id == Tender.id,
                    CompanyTender.company_id == company_id,
                ),
            )
            .where(
                Tender.deleted_at.is_(None),
                Tender.merged_into_tender_id.is_(None),
            )
            .order_by(*TENDER_ORDERS.get(sort, TENDER_ORDERS[DEFAULT_TENDER_ORDER]))
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        if closing_after is not None:
            statement = statement.where(
                or_(
                    Tender.closing_at.is_(None),
                    Tender.closing_at >= closing_after,
                )
            )
        if not include_dismissed:
            statement = statement.where(
                or_(CompanyTender.id.is_(None), CompanyTender.dismissed_at.is_(None))
            )
        if saved:
            statement = statement.where(CompanyTender.saved_at.is_not(None))
        if bidding:
            statement = statement.where(CompanyTender.bidding_at.is_not(None))
        if mine:
            statement = statement.where(
                or_(
                    CompanyTender.saved_at.is_not(None),
                    CompanyTender.bidding_at.is_not(None),
                )
            )
        if query:
            pattern = like_pattern(query.strip())
            statement = statement.where(
                or_(
                    Tender.work_title.ilike(pattern, escape="\\"),
                    Tender.reference.ilike(pattern, escape="\\"),
                    Tender.authority.ilike(pattern, escape="\\"),
                    Tender.organisation_chain.ilike(pattern, escape="\\"),
                    Tender.location.ilike(pattern, escape="\\"),
                    Tender.portal_tender_id.ilike(pattern, escape="\\"),
                )
            )
        if match_patterns:
            work_clauses = []
            for pattern in match_patterns:
                work_clauses.extend(
                    (
                        Tender.work_title.ilike(pattern, escape="\\"),
                        Tender.work_type.ilike(pattern, escape="\\"),
                        Tender.location.ilike(pattern, escape="\\"),
                    )
                )
            statement = statement.where(
                or_(
                    *work_clauses,
                    Tender.source_type == "manual_upload",
                    CompanyTender.saved_at.is_not(None),
                    CompanyTender.bidding_at.is_not(None),
                )
            )
        return list(self.session.execute(statement).all())

    def recent_titles(self, *, limit: int = 3000) -> list[str]:
        """The newest work titles, for corpus-wide rarity.

        Rarity must be read from the corpus, not from the rows that already
        matched: a profile with one term would otherwise see that term in every
        candidate and call it generic by construction. The sample is bounded so
        the query stays cheap, and it is ordered so the answer is deterministic.
        """

        rows = self.session.scalars(
            select(Tender.work_title)
            .where(
                Tender.work_title.is_not(None),
                Tender.deleted_at.is_(None),
                Tender.merged_into_tender_id.is_(None),
            )
            .order_by(Tender.created_at.desc(), Tender.id.desc())
            .limit(limit)
        ).all()
        return [str(title) for title in rows if title]

    def create(self, values: dict[str, Any]) -> Tender:
        tender = Tender(**values)
        self.session.add(tender)
        self.session.flush()
        return tender

    def upsert_locator(
        self, tender_id: uuid.UUID, values: dict[str, Any]
    ) -> TenderLocator:
        locator = self.session.scalar(
            select(TenderLocator).where(TenderLocator.tender_id == tender_id)
        )
        if locator is None:
            locator = TenderLocator(tender_id=tender_id, **values)
            self.session.add(locator)
        else:
            for key, value in values.items():
                setattr(locator, key, value)
        self.session.flush()
        return locator

    def locator(self, tender_id: uuid.UUID) -> TenderLocator | None:
        return self.session.scalar(
            select(TenderLocator).where(TenderLocator.tender_id == tender_id)
        )

    def touch(self, tender: Tender) -> None:
        tender.updated_at = utcnow()
        self.session.flush()

    def mark_terminated(
        self, tender: Tender, *, excerpt: str, observed_at: datetime
    ) -> Tender:
        """Record that the portal states this bidding cycle is over.

        The excerpt is the portal's own sentence, kept as the evidence for the
        change. It is never replaced by a guess: a called-off cycle is only
        recorded when the source says so.
        """

        tender.cycle_state = "terminated"
        tender.termination_excerpt = (excerpt or "")[:300] or None
        tender.termination_observed_at = observed_at
        tender.updated_at = utcnow()
        self.session.flush()
        return tender


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, document_id: uuid.UUID) -> TenderDocument | None:
        return self.session.scalar(
            select(TenderDocument).where(
                TenderDocument.id == document_id,
                TenderDocument.deleted_at.is_(None),
            )
        )

    def find_by_sha(self, tender_id: uuid.UUID, sha256: str) -> TenderDocument | None:
        return self.session.scalar(
            select(TenderDocument).where(
                TenderDocument.tender_id == tender_id,
                TenderDocument.sha256 == sha256,
                TenderDocument.deleted_at.is_(None),
            )
        )

    def current_for_key(
        self, tender_id: uuid.UUID, key: str
    ) -> TenderDocument | None:
        return self.session.scalar(
            select(TenderDocument).where(
                TenderDocument.tender_id == tender_id,
                TenderDocument.document_key == key,
                TenderDocument.is_current.is_(True),
                TenderDocument.deleted_at.is_(None),
            )
        )

    def latest_version(self, tender_id: uuid.UUID, key: str) -> TenderDocument | None:
        return self.session.scalar(
            select(TenderDocument)
            .where(
                TenderDocument.tender_id == tender_id,
                TenderDocument.document_key == key,
            )
            .order_by(TenderDocument.created_at.desc())
            .limit(1)
        )

    def list_for_tender(
        self, tender_id: uuid.UUID, *, include_non_current: bool = False
    ) -> list[TenderDocument]:
        statement = select(TenderDocument).where(
            TenderDocument.tender_id == tender_id,
            TenderDocument.deleted_at.is_(None),
        )
        if not include_non_current:
            statement = statement.where(TenderDocument.is_current.is_(True))
        statement = statement.order_by(
            TenderDocument.created_at.asc(), TenderDocument.version.asc()
        )
        return list(self.session.scalars(statement).all())

    def create(self, values: dict[str, Any]) -> TenderDocument:
        document = TenderDocument(**values)
        self.session.add(document)
        self.session.flush()
        return document

    def pages(self, document_id: uuid.UUID) -> list[DocumentPage]:
        return list(
            self.session.scalars(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_index.asc())
            ).all()
        )

    def page(self, document_id: uuid.UUID, page_index: int) -> DocumentPage | None:
        return self.session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document_id,
                DocumentPage.page_index == page_index,
            )
        )

    def add_page(
        self,
        *,
        document_id: uuid.UUID,
        page_index: int,
        printed_label: str | None,
        text: str,
        char_count: int,
        is_ocr: bool,
    ) -> DocumentPage:
        page = DocumentPage(
            document_id=document_id,
            page_index=page_index,
            printed_label=printed_label,
            text=text,
            char_count=char_count,
            is_ocr=is_ocr,
        )
        self.session.add(page)
        return page

    def count_current(self, tender_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(TenderDocument.id)).where(
                    TenderDocument.tender_id == tender_id,
                    TenderDocument.is_current.is_(True),
                    TenderDocument.deleted_at.is_(None),
                )
            )
            or 0
        )


class RequirementRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, requirement_id: uuid.UUID) -> Requirement | None:
        return self.session.scalar(
            select(Requirement)
            .options(joinedload(Requirement.document))
            .where(Requirement.id == requirement_id, Requirement.is_active.is_(True))
        )

    def get_for_document(
        self, document_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> Requirement | None:
        return self.session.scalar(
            select(Requirement).where(
                Requirement.id == requirement_id,
                Requirement.document_id == document_id,
            )
        )

    def exists(
        self, document_id: uuid.UUID, page_index: int, digest: str
    ) -> bool:
        return (
            self.session.scalar(
                select(Requirement.id)
                .where(
                    Requirement.document_id == document_id,
                    Requirement.page_index == page_index,
                    Requirement.excerpt_hash == digest,
                    Requirement.is_active.is_(True),
                )
                .limit(1)
            )
            is not None
        )

    def list_for_document(
        self, document_id: uuid.UUID, *, active_only: bool = True
    ) -> list[Requirement]:
        statement = select(Requirement).where(Requirement.document_id == document_id)
        if active_only:
            statement = statement.where(Requirement.is_active.is_(True))
        statement = statement.order_by(
            Requirement.page_index.asc(), Requirement.created_at.asc()
        )
        return list(self.session.scalars(statement).all())

    def list_for_tender(
        self,
        tender_id: uuid.UUID,
        *,
        state: list[str] | None = None,
        category: str | None = None,
        include_navigation: bool = False,
        include_inactive: bool = False,
        company_id: uuid.UUID | None = None,
        include_ignored: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[Requirement], int]:
        statement = (
            select(Requirement)
            .join(TenderDocument, Requirement.document_id == TenderDocument.id)
            .options(joinedload(Requirement.document))
            .where(
                TenderDocument.tender_id == tender_id,
                TenderDocument.deleted_at.is_(None),
            )
        )
        if company_id is not None and not include_ignored:
            statement = statement.outerjoin(
                CompanyRequirementFlag,
                and_(
                    CompanyRequirementFlag.requirement_id == Requirement.id,
                    CompanyRequirementFlag.company_id == company_id,
                ),
            ).where(
                or_(
                    CompanyRequirementFlag.id.is_(None),
                    CompanyRequirementFlag.ignored.is_(False),
                )
            )
        if not include_inactive:
            statement = statement.where(Requirement.is_active.is_(True))
        if not include_navigation:
            statement = statement.where(Requirement.is_navigation.is_(False))
        if state:
            statement = statement.where(Requirement.state.in_(state))
        if category:
            statement = statement.where(Requirement.category == category)
        total = int(
            self.session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            or 0
        )
        statement = (
            statement.order_by(
                Requirement.page_index.asc(),
                Requirement.created_at.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement).all()), total

    def create(self, values: dict[str, Any]) -> Requirement:
        requirement = Requirement(**values)
        self.session.add(requirement)
        self.session.flush()
        return requirement

    def deactivate_ai(self, document_id: uuid.UUID) -> list[uuid.UUID]:
        rows = list(
            self.session.scalars(
                select(Requirement).where(
                    Requirement.document_id == document_id,
                    Requirement.origin == "ai",
                    Requirement.is_active.is_(True),
                )
            ).all()
        )
        ids = [row.id for row in rows]
        for row in rows:
            row.is_active = False
        self.session.flush()
        return ids

    def reactivate(self, ids: list[uuid.UUID]) -> None:
        if not ids:
            return
        for row in self.session.scalars(
            select(Requirement).where(Requirement.id.in_(ids))
        ).all():
            row.is_active = True
        self.session.flush()

    def summary(self, tender_id: uuid.UUID) -> dict[str, int]:
        rows = self.session.execute(
            select(Requirement.state, func.count(Requirement.id))
            .join(TenderDocument, Requirement.document_id == TenderDocument.id)
            .where(
                TenderDocument.tender_id == tender_id,
                TenderDocument.deleted_at.is_(None),
                Requirement.is_active.is_(True),
            )
            .group_by(Requirement.state)
        ).all()
        counts = {
            "verified": 0,
            "needs_review": 0,
            "accepted_ocr": 0,
            "unverified": 0,
            "rejected": 0,
        }
        for state, count in rows:
            counts[state] = int(count)
        counts["total"] = sum(counts.values())
        return counts

    def count_active(self, tender_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(Requirement.id))
                .join(TenderDocument, Requirement.document_id == TenderDocument.id)
                .where(
                    TenderDocument.tender_id == tender_id,
                    TenderDocument.deleted_at.is_(None),
                    Requirement.is_active.is_(True),
                )
            )
            or 0
        )


class TenderAuxRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def covers(self, tender_id: uuid.UUID) -> list[TenderCover]:
        return list(
            self.session.scalars(
                select(TenderCover)
                .where(TenderCover.tender_id == tender_id)
                .order_by(TenderCover.version.asc(), TenderCover.cover_no.asc())
            ).all()
        )

    def replace_covers(
        self,
        tender_id: uuid.UUID,
        version: str,
        rows: list[dict[str, Any]],
    ) -> list[TenderCover]:
        """Replace one version of the portal's cover table.

        A cover table is the portal's own expected-document list. It is stored
        as read; matching it to files is a report, never a reason to fail.
        """

        for existing in self.session.scalars(
            select(TenderCover).where(
                TenderCover.tender_id == tender_id,
                TenderCover.version == version,
            )
        ).all():
            self.session.delete(existing)
        stored: list[TenderCover] = []
        for row in rows:
            cover = TenderCover(
                tender_id=tender_id,
                version=version,
                cover_no=int(row["cover_no"]),
                doc_type=row.get("doc_type"),
                description=row.get("description"),
            )
            self.session.add(cover)
            stored.append(cover)
        self.session.flush()
        return stored

    def fetch_state(self, tender_id: uuid.UUID) -> DocumentFetchState | None:
        return self.session.scalar(
            select(DocumentFetchState).where(
                DocumentFetchState.tender_id == tender_id
            )
        )

    def ensure_fetch_state(
        self, tender_id: uuid.UUID, *, documents_present: int
    ) -> DocumentFetchState:
        state = self.fetch_state(tender_id)
        if state is None:
            state = DocumentFetchState(
                tender_id=tender_id, documents_present=documents_present
            )
            self.session.add(state)
        else:
            state.documents_present = documents_present
            state.updated_at = utcnow()
        self.session.flush()
        return state


class RequirementFlagRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, company_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> CompanyRequirementFlag | None:
        return self.session.scalar(
            select(CompanyRequirementFlag).where(
                CompanyRequirementFlag.company_id == company_id,
                CompanyRequirementFlag.requirement_id == requirement_id,
            )
        )

    def upsert(
        self,
        *,
        company_id: uuid.UUID,
        requirement_id: uuid.UUID,
        ignored: bool | None = None,
        note: str | None = None,
        set_note: bool = False,
    ) -> CompanyRequirementFlag:
        row = self.get(company_id, requirement_id)
        if row is None:
            row = CompanyRequirementFlag(
                company_id=company_id,
                requirement_id=requirement_id,
                ignored=bool(ignored),
                note=note,
            )
            self.session.add(row)
        else:
            if ignored is not None:
                row.ignored = ignored
            if set_note:
                row.note = note
            row.updated_at = utcnow()
        self.session.flush()
        return row
