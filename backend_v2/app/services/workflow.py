from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.core.time import utcnow
from app.models.corpus import Requirement, Tender
from app.models.workflow import Change, TenderSnapshot
from app.repositories.corpus import RequirementRepository, TenderRepository
from app.repositories.identity import AuditRepository
from app.repositories.workflow import ChangeRepository, SnapshotRepository

SNAPSHOT_FIELDS = (
    "work_title",
    "authority",
    "organisation_chain",
    "reference",
    "closing_at",
    "published_at",
    "value_amount",
    "currency",
    "location",
    "region",
    "work_type",
    "source_url",
    "source_type",
    "extracted_from",
    "unverified",
    "supersedes_tender_id",
    "supersedes_state",
)

FIELD_LABELS = {
    "work_title": "Work title",
    "authority": "Authority",
    "organisation_chain": "Organisation",
    "reference": "Reference",
    "closing_at": "Closing date",
    "published_at": "Published date",
    "value_amount": "Value",
    "currency": "Currency",
    "location": "Location",
    "region": "Region",
    "work_type": "Work type",
    "source_url": "Source URL",
    "source_type": "Source type",
    "extracted_from": "Extracted from",
    "unverified": "Unverified fields",
    "supersedes_tender_id": "Supersedes tender",
    "supersedes_state": "Lineage state",
}

WARNING_FIELDS = {
    "closing_at",
    "closing_date",
    "reference",
    "work_title",
    "source_url",
    "supersedes_tender_id",
    "supersedes_state",
}

LINEAGE_FIELDS = {"supersedes_tender_id", "supersedes_state"}


def _display(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (list, tuple, set)):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(cleaned) if cleaned else None
    text = str(value).strip()
    return text or None


def _summary(field_key: str, old: str | None, new: str | None) -> str:
    label = FIELD_LABELS.get(field_key, "Requirement")
    if field_key.startswith("req:"):
        return "Requirement text changed"
    if old is None:
        return f"{label} added: {new}" if new else f"{label} added"
    if new is None:
        return f"{label} removed (was {old})"
    return f"{label}: {old} -> {new}"


def _fingerprint(requirement: Requirement) -> dict[str, Any]:
    return {
        "requirement_id": str(requirement.id),
        "title": requirement.title,
        "excerpt": (requirement.excerpt or "")[:500],
        "excerpt_hash": requirement.excerpt_hash,
        "state": requirement.state,
        "category": requirement.category,
        "mandatory_state": requirement.mandatory_state,
        "page_index": requirement.page_index,
        "document_id": str(requirement.document_id),
    }


def _fingerprint_key(item: dict[str, Any]) -> tuple[str, str | None, str]:
    return (
        str(item.get("requirement_id")),
        item.get("title"),
        str(item.get("excerpt_hash")),
    )


class WorkflowService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.tenders = TenderRepository(session)
        self.requirements = RequirementRepository(session)
        self.snapshots = SnapshotRepository(session)
        self.changes = ChangeRepository(session)
        self.audit = AuditRepository(session)

    # -- snapshots --------------------------------------------------------

    def _tender(self, tender_id: uuid.UUID) -> Tender:
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        return tender

    def _fields(self, tender: Tender) -> dict[str, str | None]:
        return {field: _display(getattr(tender, field, None)) for field in SNAPSHOT_FIELDS}

    def _fingerprints(self, tender_id: uuid.UUID) -> list[dict[str, Any]]:
        rows, _ = self.requirements.list_for_tender(
            tender_id, include_navigation=False, include_inactive=False, limit=5000
        )
        return [_fingerprint(row) for row in rows]

    @staticmethod
    def _content_hash(
        fields: dict[str, Any], fingerprints: list[dict[str, Any]]
    ) -> str:
        # Human review state is deliberately excluded: a person changing a
        # requirement from needs_review to accepted_ocr is not a tender change.
        payload = {
            "fields": fields,
            "requirements": sorted(
                (
                    {
                        "requirement_id": item.get("requirement_id"),
                        "title": item.get("title"),
                        "excerpt_hash": item.get("excerpt_hash"),
                    }
                    for item in fingerprints
                ),
                key=lambda item: str(item["requirement_id"]),
            ),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _after(previous: TenderSnapshot | None, now: datetime) -> datetime:
        if previous is not None and previous.captured_at is not None:
            if now <= previous.captured_at:
                return previous.captured_at + timedelta(milliseconds=1)
        return now

    def capture_snapshot(
        self,
        tender_id: uuid.UUID,
        *,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> tuple[TenderSnapshot, bool, list[Change]]:
        """Capture one snapshot and the deterministic diff from its predecessor.

        Returns ``(snapshot, created, changes)``. Calling this twice without a
        change returns the existing latest snapshot and creates nothing.
        """

        tender = self._tender(tender_id)
        fields = self._fields(tender)
        fingerprints = self._fingerprints(tender_id)
        content_hash = self._content_hash(fields, fingerprints)
        previous = self.snapshots.latest(tender_id)

        if previous is not None and previous.content_hash == content_hash:
            return previous, False, []

        captured_at = self._after(previous, utcnow())
        try:
            snapshot = self.snapshots.create(
                {
                    "tender_id": tender.id,
                    "captured_at": captured_at,
                    "content_hash": content_hash,
                    "fields": fields,
                    "requirement_fingerprints": fingerprints,
                }
            )
            created_changes = self._diff(
                tender=tender,
                previous=previous,
                current=snapshot,
            )
            self.audit.log(
                action="tender.snapshot_captured",
                entity_type="tender",
                company_id=company_id,
                actor_user_id=actor_user_id,
                entity_id=tender.id,
                after={
                    "snapshot_id": str(snapshot.id),
                    "captured_at": captured_at.isoformat(),
                    "content_hash": content_hash,
                    "changes": len(created_changes),
                },
            )
            if commit:
                self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return snapshot, True, created_changes

    def list_snapshots(
        self, tender_id: uuid.UUID, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[TenderSnapshot], int]:
        self._tender(tender_id)
        return (
            self.snapshots.list_for_tender(tender_id, limit=limit, offset=offset),
            self.snapshots.count_for_tender(tender_id),
        )

    def _diff(
        self,
        *,
        tender: Tender,
        previous: TenderSnapshot | None,
        current: TenderSnapshot,
    ) -> list[Change]:
        if previous is None:
            return []
        rows: list[dict[str, Any]] = []
        old_fields = previous.fields or {}
        new_fields = current.fields or {}
        for key in SNAPSHOT_FIELDS:
            old_value = old_fields.get(key)
            new_value = new_fields.get(key)
            if old_value == new_value:
                continue
            change_type = "lineage_linked" if key in LINEAGE_FIELDS else "field_changed"
            rows.append(
                {
                    "tender_id": tender.id,
                    "from_snapshot_id": previous.id,
                    "to_snapshot_id": current.id,
                    "change_type": change_type,
                    "field_key": key,
                    "old_value": old_value,
                    "new_value": new_value,
                    "detected_at": utcnow(),
                    "review_state": "pending",
                    "source_url": tender.source_url,
                }
            )

        old_items = {
            str(item.get("requirement_id")): item
            for item in (previous.requirement_fingerprints or [])
        }
        new_items = {
            str(item.get("requirement_id")): item
            for item in (current.requirement_fingerprints or [])
        }
        for requirement_id in sorted(set(old_items) - set(new_items)):
            old = old_items[requirement_id]
            rows.append(
                self._requirement_change(
                    tender=tender,
                    previous=previous,
                    current=current,
                    change_type="requirement_removed",
                    requirement_id=requirement_id,
                    old=old,
                    new=None,
                )
            )
        for requirement_id in sorted(set(new_items) - set(old_items)):
            new = new_items[requirement_id]
            rows.append(
                self._requirement_change(
                    tender=tender,
                    previous=previous,
                    current=current,
                    change_type="requirement_added",
                    requirement_id=requirement_id,
                    old=None,
                    new=new,
                )
            )
        for requirement_id in sorted(set(old_items) & set(new_items)):
            old = old_items[requirement_id]
            new = new_items[requirement_id]
            # State, category and mandatory flags are excluded on purpose. Only
            # the cited text and its page identity are tender facts.
            if _fingerprint_key(old) == _fingerprint_key(new):
                continue
            rows.append(
                self._requirement_change(
                    tender=tender,
                    previous=previous,
                    current=current,
                    change_type="requirement_changed",
                    requirement_id=requirement_id,
                    old=old,
                    new=new,
                )
            )
        return self.changes.create_many(rows)

    def _requirement_change(
        self,
        *,
        tender: Tender,
        previous: TenderSnapshot,
        current: TenderSnapshot,
        change_type: str,
        requirement_id: str,
        old: dict[str, Any] | None,
        new: dict[str, Any] | None,
    ) -> dict[str, Any]:
        old_value = None
        new_value = None
        if old is not None:
            old_value = old.get("title") or old.get("excerpt")
        if new is not None:
            new_value = new.get("title") or new.get("excerpt")
        return {
            "tender_id": tender.id,
            "from_snapshot_id": previous.id,
            "to_snapshot_id": current.id,
            "change_type": change_type,
            "field_key": f"req:{requirement_id}",
            "old_value": old_value,
            "new_value": new_value,
            "detected_at": utcnow(),
            "review_state": "pending",
            "source_url": tender.source_url,
        }

    # -- changes ----------------------------------------------------------

    def list_changes(
        self,
        tender_id: uuid.UUID,
        *,
        review_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Change], int]:
        self._tender(tender_id)
        return self.changes.list_for_tender(
            tender_id, review_state=review_state, limit=limit, offset=offset
        )

    def list_company_changes(
        self,
        company_id: uuid.UUID,
        *,
        saved_only: bool = True,
        review_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Change], int]:
        return self.changes.list_for_company(
            company_id,
            saved_only=saved_only,
            review_state=review_state,
            limit=limit,
            offset=offset,
        )

    def review_change(
        self,
        change_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        review_state: str,
    ) -> Change:
        change = self.changes.get(change_id)
        if change is None:
            raise NotFound("Change not found.")
        before = change.review_state
        try:
            self.changes.review(
                change,
                review_state=review_state,
                reviewed_by=actor_user_id,
                reviewed_at=utcnow(),
            )
            self.audit.log(
                company_id=company_id,
                actor_user_id=actor_user_id,
                action="change.reviewed",
                entity_type="change",
                entity_id=change.id,
                before={"review_state": before},
                after={"review_state": review_state},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return change

    # -- corrigenda lineage ----------------------------------------------

    def _lineage_candidate(self, tender: Tender) -> Tender | None:
        if (
            tender.reference_normalised is None
            or tender.work_title is None
            or tender.published_at is None
        ):
            return None
        if not (tender.organisation_chain or tender.authority):
            return None

        statement = select(Tender).where(
            Tender.id != tender.id,
            Tender.deleted_at.is_(None),
            Tender.merged_into_tender_id.is_(None),
            Tender.reference_normalised == tender.reference_normalised,
            func.lower(Tender.work_title) == tender.work_title.casefold(),
            Tender.published_at.is_not(None),
            Tender.published_at < tender.published_at,
        )
        if tender.portal_id is None:
            statement = statement.where(Tender.portal_id.is_(None))
        else:
            statement = statement.where(Tender.portal_id == tender.portal_id)
        if tender.organisation_chain:
            statement = statement.where(
                func.lower(Tender.organisation_chain)
                == tender.organisation_chain.casefold()
            )
        else:
            statement = statement.where(
                func.lower(Tender.authority) == (tender.authority or "").casefold()
            )
        return self.session.scalar(
            statement.order_by(Tender.published_at.desc(), Tender.id.desc()).limit(1)
        )

    def propose_lineage(
        self,
        tender_id: uuid.UUID,
        *,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> Tender:
        """Propose a corrigendum link only when every identity field agrees.

        A shared reference alone is never enough: portal, normalised reference,
        organisation, work title and a later publication date must all match.
        The result is a proposal, never an automatic confirmation.
        """

        tender = self._tender(tender_id)
        if tender.supersedes_tender_id is not None:
            return tender
        candidate = self._lineage_candidate(tender)
        if candidate is None:
            return tender
        try:
            tender.supersedes_tender_id = candidate.id
            tender.supersedes_state = "proposed"
            tender.updated_at = utcnow()
            self.session.flush()
            self.capture_snapshot(
                tender.id,
                company_id=company_id,
                actor_user_id=actor_user_id,
                commit=False,
            )
            self.audit.log(
                action="tender.lineage_proposed",
                entity_type="tender",
                company_id=company_id,
                actor_user_id=actor_user_id,
                entity_id=tender.id,
                after={
                    "supersedes_tender_id": str(candidate.id),
                    "supersedes_state": "proposed",
                },
            )
            if commit:
                self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return tender

    def _require_proposal(self, tender_id: uuid.UUID, candidate_id: uuid.UUID) -> Tender:
        tender = self._tender(tender_id)
        if tender.supersedes_tender_id is None:
            raise NotFound("This tender has no proposed corrigendum link.")
        if tender.supersedes_tender_id != candidate_id:
            raise NotFound("That corrigendum proposal is not attached to this tender.")
        return tender

    def confirm_lineage(
        self,
        tender_id: uuid.UUID,
        candidate_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> Tender:
        tender = self._require_proposal(tender_id, candidate_id)
        if tender.supersedes_state == "confirmed":
            return tender
        before = tender.supersedes_state
        try:
            tender.supersedes_state = "confirmed"
            tender.updated_at = utcnow()
            self.session.flush()
            self.capture_snapshot(
                tender.id,
                company_id=company_id,
                actor_user_id=actor_user_id,
                commit=False,
            )
            self.audit.log(
                action="tender.lineage_confirmed",
                entity_type="tender",
                company_id=company_id,
                actor_user_id=actor_user_id,
                entity_id=tender.id,
                before={"supersedes_state": before},
                after={
                    "supersedes_tender_id": str(tender.supersedes_tender_id),
                    "supersedes_state": "confirmed",
                },
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return tender

    def reject_lineage(
        self,
        tender_id: uuid.UUID,
        candidate_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> Tender:
        tender = self._require_proposal(tender_id, candidate_id)
        if tender.supersedes_state == "rejected":
            return tender
        if tender.supersedes_state == "confirmed":
            raise Conflict("A confirmed corrigendum link cannot be rejected.")
        before = tender.supersedes_state
        try:
            tender.supersedes_state = "rejected"
            tender.updated_at = utcnow()
            self.session.flush()
            self.capture_snapshot(
                tender.id,
                company_id=company_id,
                actor_user_id=actor_user_id,
                commit=False,
            )
            self.audit.log(
                action="tender.lineage_rejected",
                entity_type="tender",
                company_id=company_id,
                actor_user_id=actor_user_id,
                entity_id=tender.id,
                before={"supersedes_state": before},
                after={
                    "supersedes_tender_id": str(tender.supersedes_tender_id),
                    "supersedes_state": "rejected",
                },
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return tender

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def summary(change: Change) -> str:
        return _summary(change.field_key, change.old_value, change.new_value)


def change_is_warning(change: Change) -> bool:
    if change.change_type in {"requirement_added", "requirement_removed", "requirement_changed"}:
        return True
    return change.field_key in WARNING_FIELDS
