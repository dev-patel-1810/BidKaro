from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.intelligence import (
    CompanyRelevanceFeedback,
    CompanyTenderVerdict,
    TenderClassification,
    TenderObservation,
)


class ObservationRepository:
    """Append-only trail of where a tender was seen."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        route_key: str,
        outcome: str,
        tender_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        portal_code: str = "cppp",
        organisation: str | None = None,
        listing_url: str | None = None,
        list_page: int | None = None,
        row_position: int | None = None,
        portal_tender_id: str | None = None,
        reference: str | None = None,
        href: str | None = None,
        session_ref: str | None = None,
        reason: str | None = None,
    ) -> TenderObservation:
        observation = TenderObservation(
            tender_id=tender_id,
            run_id=run_id,
            portal_code=portal_code,
            organisation=organisation,
            listing_url=listing_url,
            list_page=list_page,
            row_position=row_position,
            portal_tender_id=portal_tender_id,
            reference=reference,
            route_key=route_key,
            href=href,
            session_ref=session_ref,
            outcome=outcome,
            reason=reason,
            observed_at=utcnow(),
        )
        self.session.add(observation)
        self.session.flush()
        return observation

    def for_tender(
        self, tender_id: uuid.UUID, *, limit: int = 20
    ) -> list[TenderObservation]:
        return list(
            self.session.scalars(
                select(TenderObservation)
                .where(TenderObservation.tender_id == tender_id)
                .order_by(TenderObservation.observed_at.desc())
                .limit(limit)
            ).all()
        )

    def last_seen(self, tender_id: uuid.UUID) -> TenderObservation | None:
        return self.session.scalar(
            select(TenderObservation)
            .where(
                TenderObservation.tender_id == tender_id,
                TenderObservation.outcome.in_(("stored", "updated")),
            )
            .order_by(TenderObservation.observed_at.desc())
            .limit(1)
        )


class ClassificationRepository:
    """Company-independent work-type labels, one per tender."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, tender_id: uuid.UUID) -> TenderClassification | None:
        return self.session.get(TenderClassification, tender_id)

    def get_many(
        self, tender_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, TenderClassification]:
        ids = list(tender_ids)
        if not ids:
            return {}
        rows = self.session.scalars(
            select(TenderClassification).where(
                TenderClassification.tender_id.in_(ids)
            )
        ).all()
        return {row.tender_id: row for row in rows}

    def upsert(
        self,
        tender_id: uuid.UUID,
        *,
        primary_work_type: str | None,
        work_types: list[str],
        confidence: float | None,
        method: str,
        rules_version: str,
        input_hash: str,
        reason: str | None = None,
    ) -> TenderClassification:
        row = self.session.get(TenderClassification, tender_id)
        if row is None:
            row = TenderClassification(tender_id=tender_id)
            self.session.add(row)
        row.primary_work_type = primary_work_type
        row.work_types = work_types
        row.confidence = confidence
        row.method = method
        row.rules_version = rules_version
        row.input_hash = input_hash
        row.reason = reason
        row.updated_at = utcnow()
        self.session.flush()
        return row


class VerdictRepository:
    """Per-company relevance verdicts, reuseable while their inputs match."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, company_id: uuid.UUID, tender_id: uuid.UUID
    ) -> CompanyTenderVerdict | None:
        return self.session.scalar(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.company_id == company_id,
                CompanyTenderVerdict.tender_id == tender_id,
            )
        )

    def get_many(
        self, company_id: uuid.UUID, tender_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, CompanyTenderVerdict]:
        ids = list(tender_ids)
        if not ids:
            return {}
        rows = self.session.scalars(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.company_id == company_id,
                CompanyTenderVerdict.tender_id.in_(ids),
            )
        ).all()
        return {row.tender_id: row for row in rows}

    def upsert(
        self,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        *,
        bucket: str,
        tier: int = 7,
        score: float,
        matched_terms: list[str],
        reasons: list[str],
        source: str,
        input_hash: str,
        rules_version: str,
    ) -> CompanyTenderVerdict:
        row = self.get(company_id, tender_id)
        if row is None:
            row = CompanyTenderVerdict(company_id=company_id, tender_id=tender_id)
            self.session.add(row)
        row.bucket = bucket
        row.tier = tier
        row.score = score
        row.matched_terms = matched_terms
        row.reasons = reasons
        row.source = source
        row.input_hash = input_hash
        row.rules_version = rules_version
        row.updated_at = utcnow()
        self.session.flush()
        return row


class FeedbackRepository:
    """The human signal: saved is positive, "not relevant" is negative."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, company_id: uuid.UUID, tender_id: uuid.UUID
    ) -> CompanyRelevanceFeedback | None:
        return self.session.scalar(
            select(CompanyRelevanceFeedback).where(
                CompanyRelevanceFeedback.company_id == company_id,
                CompanyRelevanceFeedback.tender_id == tender_id,
            )
        )

    def upsert(
        self,
        company_id: uuid.UUID,
        tender_id: uuid.UUID,
        *,
        verdict: str,
        work_types: list[str] | None = None,
        matched_terms: list[str] | None = None,
    ) -> CompanyRelevanceFeedback:
        row = self.get(company_id, tender_id)
        if row is None:
            row = CompanyRelevanceFeedback(company_id=company_id, tender_id=tender_id)
            self.session.add(row)
        row.verdict = verdict
        row.work_types = work_types or []
        row.matched_terms = matched_terms or []
        row.created_at = utcnow()
        self.session.flush()
        return row

    def delete(self, company_id: uuid.UUID, tender_id: uuid.UUID) -> None:
        """Withdraw this company's signal about one tender.

        An unsaved tender is no longer evidence about the company's work: the
        "saved" verdict it recorded must stop weighting future matches, and a
        row deleted here leaves the tender as unjudged.
        """

        row = self.get(company_id, tender_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def negative_work_types(
        self, company_id: uuid.UUID, *, limit: int | None = None
    ) -> dict[str, int]:
        """How often the company rejected each kind of work.

        Both the classified work types and the profile terms that made the
        tender match are counted, because before a tender is classified the
        matched terms are the only description of its work. Used as a small
        negative weight, never as a ban: a word that once appeared in a
        rejected tender must still be able to match a different tender. Every
        rejection counts; there is no sample ceiling, because a term rejected
        after the cap would otherwise silently stop demoting.
        """

        statement = (
            select(CompanyRelevanceFeedback)
            .where(
                CompanyRelevanceFeedback.company_id == company_id,
                CompanyRelevanceFeedback.verdict == "not_relevant",
            )
            .order_by(CompanyRelevanceFeedback.created_at.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = self.session.scalars(statement).all()
        counts: dict[str, int] = {}
        for row in rows:
            for label in list(row.work_types or []) + list(row.matched_terms or []):
                if not label:
                    continue
                counts[label] = counts.get(label, 0) + 1
        return counts

    def list_for_company(
        self,
        company_id: uuid.UUID,
        *,
        verdict: str | None = None,
        limit: int | None = None,
    ) -> list[CompanyRelevanceFeedback]:
        statement = select(CompanyRelevanceFeedback).where(
            CompanyRelevanceFeedback.company_id == company_id
        )
        if verdict:
            statement = statement.where(CompanyRelevanceFeedback.verdict == verdict)
        statement = statement.order_by(CompanyRelevanceFeedback.created_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement).all())


def verdict_input_hash(
    *,
    profile_hash: str,
    tender_content_hash: str,
    feedback_hash: str,
    rules_version: str,
) -> str:
    """One hash over every input that can change a work-type verdict.

    A profile edit, a save or a "not relevant", a re-published tender, or an
    improved matcher each change this hash, which is what makes a stored
    verdict safe to reuse and honest to discard. Evidence is deliberately not
    an input: a work-type verdict is about whether the company does the work,
    not whether it can prove eligibility.
    """

    joined = "|".join(
        [
            profile_hash or "",
            tender_content_hash or "",
            feedback_hash or "",
            rules_version or "",
        ]
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def stable_hash(values: Any) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def tender_content_hash(tender: Any) -> str:
    """The part of a tender a verdict depends on.

    One function, used by the background pass and the read path, so a stored
    verdict is reused only when it was computed from the same tender. If the
    two sides hashed different fields, every read would look like a cache miss
    and the cache would be dead weight.
    """

    return stable_hash(
        [
            getattr(tender, "work_title", None),
            getattr(tender, "work_type", None),
            getattr(tender, "location", None),
            getattr(tender, "reference", None),
            getattr(tender, "closing_at", None),
        ]
    )
