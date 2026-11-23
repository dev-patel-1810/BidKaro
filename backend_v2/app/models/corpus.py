from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Portal(Base):
    __tablename__ = "portals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Tender(Base):
    __tablename__ = "tenders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    portal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portals.id", ondelete="RESTRICT")
    )
    portal_tender_id: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    reference: Mapped[str | None] = mapped_column(Text)
    reference_normalised: Mapped[str | None] = mapped_column(Text)
    authority: Mapped[str | None] = mapped_column(Text)
    organisation_chain: Mapped[str | None] = mapped_column(Text)
    work_title: Mapped[str | None] = mapped_column(Text)
    work_type: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    closing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'INR'")
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_from: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(Text)
    cycle_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'active'")
    )
    termination_excerpt: Mapped[str | None] = mapped_column(Text)
    termination_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    unverified: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]")
    )
    supersedes_tender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL")
    )
    supersedes_state: Mapped[str | None] = mapped_column(Text)
    merged_into_tender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL")
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cross_portal_key: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    documents: Mapped[list[TenderDocument]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )


class TenderLocator(Base):
    """How to find this tender again: portal + portal tender id.

    The notice URL carries a session token and expires, so it is evidence of a
    read, never the identifier. The locator is what lets a later fetch happen
    days after the walk that found the tender.
    """

    __tablename__ = "tender_locators"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    portal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portals.id", ondelete="RESTRICT"), nullable=False
    )
    portal_tender_id: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    organisation_chain: Mapped[str | None] = mapped_column(Text)
    work_title: Mapped[str | None] = mapped_column(Text)
    published_on: Mapped[date | None] = mapped_column(Date)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'v1'")
    )
    document_key: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tender_documents.id", ondelete="SET NULL")
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    has_text_layer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    ocr_page_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    extraction_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'pending'")
    )
    extraction_error: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tender: Mapped[Tender] = relationship(back_populates="documents")
    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tender_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_label: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    char_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    is_ocr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[TenderDocument] = relationship(back_populates="pages")


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tender_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_label: Mapped[str | None] = mapped_column(Text)
    clause_ref: Mapped[str | None] = mapped_column(Text)
    form_ref: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'pre_award'")
    )
    title: Mapped[str | None] = mapped_column(Text)
    obligation: Mapped[str | None] = mapped_column(Text)
    deadline_text: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consequence_text: Mapped[str | None] = mapped_column(Text)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)
    issuer: Mapped[str | None] = mapped_column(Text)
    signatory: Mapped[str | None] = mapped_column(Text)
    evidence_check: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_normalised: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_navigation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    state_reason: Mapped[str | None] = mapped_column(Text)
    mandatory_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'unknown'")
    )
    origin: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'ai'")
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[TenderDocument] = relationship()


class TenderCover(Base):
    __tablename__ = "tender_covers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    cover_no: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_type: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentFetchState(Base):
    __tablename__ = "document_fetch_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    last_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    expected_covers: Mapped[int | None] = mapped_column(Integer)
    documents_present: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    missing_covers: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=sql_text("'{}'::integer[]")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyRequirementFlag(Base):
    __tablename__ = "company_requirement_flags"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("requirements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ignored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DiscoveryCandidate(Base):
    """Every URL a crawl saw, including the ones it rejected.

    A rejection is a row, not a count: a run that read and discarded must be
    distinguishable from a run that found nothing.
    """

    __tablename__ = "discovery_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    listing_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

