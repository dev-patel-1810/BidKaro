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
    UniqueConstraint,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str | None] = mapped_column(Text)
    sectors: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]")
    )
    services: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]")
    )
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]")
    )
    profile: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    reminder_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    reminder_lead_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=sql_text("'{7,3,1}'::integer[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    users: Mapped[list[User]] = relationship(back_populates="company")
    documents: Mapped[list[CompanyDocument]] = relationship(back_populates="company")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(Text)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    email_otp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="users")


class OtpCode(Base):
    __tablename__ = "otp_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyDocument(Base):
    __tablename__ = "company_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    issuer: Mapped[str | None] = mapped_column(Text)
    signatory: Mapped[str | None] = mapped_column(Text)
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    financial_year: Mapped[str | None] = mapped_column(Text)
    project_value_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    project_value_currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'INR'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'self_declared'")
    )
    extra: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    #: ``pending`` until the document-intel job reads the file, then ``done``,
    #: ``no_text_layer``, ``skipped``, ``unsupported`` or ``failed``. Only an
    #: ``about_company`` profile is read today; every other type stays pending
    #: and is used for eligibility only.
    extraction_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'pending'")
    )
    extraction_error: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    term_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    #: ``pending`` until the understanding pass reads the profile, then ``done``,
    #: ``no_text_layer``, ``no_ai``, ``skipped`` or ``failed``. This is separate
    #: from ``extraction_state`` because the deterministic term harvest and the
    #: cited capability/experience pass fail independently.
    understanding_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'pending'")
    )
    understanding_error: Mapped[str | None] = mapped_column(Text)
    understanding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capability_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    experience_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="documents")
    versions: Mapped[list[CompanyDocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    terms: Mapped[list[CompanyDocumentTerm]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class CompanyDocumentVersion(Base):
    __tablename__ = "company_document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[CompanyDocument] = relationship(back_populates="versions")
    pages: Mapped[list[CompanyDocumentPage]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class CompanyDocumentPage(Base):
    """The page text the company-document reader kept.

    Page text is the evidence behind an extracted work term, exactly as a
    tender page is the evidence behind a cited requirement. It is stored per
    version so re-reading a replaced file never mixes the two texts.
    """

    __tablename__ = "company_document_pages"
    __table_args__ = (
        UniqueConstraint(
            "company_document_version_id",
            "page_index",
            name="uq_company_document_pages_version_page",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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

    version: Mapped[CompanyDocumentVersion] = relationship(back_populates="pages")


class CompanyDocumentTerm(Base):
    """One work phrase or word read from an ``about_company`` document.

    A term carries the page and the verbatim line it was read from, so the
    Documents tab can show why a tender was suggested and a human can remove a
    term the reader got wrong. It is a matching signal, never eligibility.
    """

    __tablename__ = "company_document_terms"
    __table_args__ = (
        UniqueConstraint(
            "company_document_id",
            "normalized",
            name="uq_company_document_terms_document_term",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    term: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_label: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'text_rule'")
    )
    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[CompanyDocument] = relationship(back_populates="terms")
