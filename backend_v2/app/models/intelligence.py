from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TenderObservation(Base):
    """One dated sighting of a tender on the portal.

    Append-only: the sequence is the evidence. "Present at this organisation on
    Monday, absent on Tuesday" is a fact; calling it cancelled still needs the
    portal's own words.

    ``route_key`` is the durable re-entry key (organisation plus tender id, or
    reference when the id is missing). ``href`` and ``session_ref`` are kept for
    evidence only: a portal notice address is bound to the browser session that
    opened it and is stale by the next day.
    """

    __tablename__ = "tender_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL")
    )
    portal_code: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'cppp'")
    )
    organisation: Mapped[str | None] = mapped_column(Text)
    listing_url: Mapped[str | None] = mapped_column(Text)
    list_page: Mapped[int | None] = mapped_column(Integer)
    row_position: Mapped[int | None] = mapped_column(Integer)
    portal_tender_id: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    route_key: Mapped[str] = mapped_column(Text, nullable=False)
    href: Mapped[str | None] = mapped_column(Text)
    session_ref: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenderClassification(Base):
    """What kind of work a tender is - company-independent, computed once.

    Classifying the work type is the expensive step. Doing it per company would
    multiply the cost by the number of companies; doing it once and matching
    per company keeps the per-company step deterministic and cheap.

    ``input_hash`` is the tender's content hash and ``rules_version`` the
    classifier's version, so either a changed tender or an improved classifier
    invalidates the row instead of leaving a stale label.
    """

    __tablename__ = "tender_classifications"

    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    primary_work_type: Mapped[str | None] = mapped_column(Text)
    work_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'rules'")
    )
    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyTenderVerdict(Base):
    """How one tender reads for one company, stamped by the inputs used.

    A verdict is not a property of the tender, so it is never global. It is
    reused only while ``input_hash`` still matches the company's profile,
    evidence, feedback and the rules version; otherwise it is recomputed.
    """

    __tablename__ = "company_tender_verdicts"
    __table_args__ = (
        UniqueConstraint("company_id", "tender_id", name="uq_company_tender_verdicts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("7")
    )
    score: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default=text("0")
    )
    matched_terms: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    reasons: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'rules'")
    )
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyRelevanceFeedback(Base):
    """A human signal about one tender: "relevant" or "not relevant".

    Saving records the positive signal; the "not relevant" control records the
    negative one. The negative signal demotes similar work, it never bans a
    word, and it is kept separately from the dismissal so the two facts can be
    used independently later.
    """

    __tablename__ = "company_relevance_feedback"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "tender_id", name="uq_company_relevance_feedback"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    work_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    matched_terms: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# This module points at tables owned by other model modules (tenders, runs,
# companies). Importing them here means a process that imports the intelligence
# models alone still has a complete metadata registry, so SQLAlchemy can order
# its inserts instead of failing on an unresolved foreign key. The imports are
# deliberately last: the classes above define the names, and these modules do
# not import this one back.
from app.models import corpus as _corpus  # noqa: E402,F401
from app.models import identity as _identity  # noqa: E402,F401
from app.models import work as _work  # noqa: E402,F401
