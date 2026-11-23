"""What the company does, what it has delivered, and how that reads against work.

Three separate records, because they answer three separate questions:

* :class:`CompanyCapability` is a *kind of work* the company itself says it
  does, read from its own profile document. Only a human-confirmed capability
  may be shown to the similarity judge.
* :class:`CompanyExperience` is a named project, contract or product the
  company delivered. It is the evidence that a requirement is ``covered``
  rather than ``new`` work.
* :class:`TenderSimilarityJudgment` is one closed-label answer (``yes``,
  ``no`` or ``unsure``) about one tender and one version of that profile. It
  is a cached read, keyed by the inputs that produced it, never a stored score.
* :class:`RequirementExperienceMatch` maps one tender requirement to the
  company's own experience, with the company-side citation copied from the
  stored page.

None of these tables can pass an eligibility criterion. Eligibility stays in
the deterministic assessment.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CompanyBrief(Base):
    """An editable, versioned statement of what the company does.

    The brief is the source of the four fit roles (core, adjacent, support and
    generic). It is generated from the company's own profile document and can
    be edited by a human; every edit is a new version, so a judgment can always
    be traced to the words that produced it. Postgres is the source of truth.
    """

    __tablename__ = "company_briefs"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "version", name="uq_company_briefs_company_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_documents.id", ondelete="SET NULL"),
    )
    #: ``generated`` from the profile, ``human`` after an edit, ``imported`` for
    #: a migration. The payload is the complete brief; no other table is needed
    #: to read it.
    source_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'generated'")
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    prompt_version: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyCapability(Base):
    """One kind of work the company says it does, with its own citation."""

    __tablename__ = "company_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "company_document_id",
            "normalized",
            name="uq_company_capabilities_document_name",
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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]")
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_label: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    quote_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    is_ocr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    #: ``proposed`` until a human confirms it, then ``confirmed`` or ``rejected``.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'proposed'")
    )
    origin: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'ai'")
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyExperience(Base):
    """A named project, contract or product the company delivered."""

    __tablename__ = "company_experiences"
    __table_args__ = (
        UniqueConstraint(
            "company_document_id",
            "normalized",
            name="uq_company_experiences_document_title",
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
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    client: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    #: The capability this delivery evidences, in the company's own words.
    work_family: Mapped[str | None] = mapped_column(Text)
    value_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'INR'")
    )
    completed_on: Mapped[date | None] = mapped_column(Date)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_label: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    quote_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    is_ocr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'proposed'")
    )
    origin: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'ai'")
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenderSimilarityJudgment(Base):
    """One closed-label answer about whether a tender is the company's work.

    ``input_hash`` covers the judged capability set, the tender's content and
    the rules versions, so a profile edit, a re-published notice or a new rules
    version re-judges while a plain repeat read reuses the row. The judgment is
    never eligibility and never hides a tender.
    """

    __tablename__ = "tender_similarity_judgments"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "tender_id",
            name="uq_tender_similarity_judgments_company_tender",
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
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``yes`` | ``no`` | ``unsure``. A ``yes`` without a verified tender-side
    #: anchor is stored as ``unsure`` with ``needs_confirmation`` set.
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_text: Mapped[str | None] = mapped_column(Text)
    anchor_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    #: The exact line quoted from the notice, and the exact line quoted from
    #: the company brief. They are validated against the stored source before
    #: they are shown; a model quote that is not found is not stored here.
    tender_evidence: Mapped[str | None] = mapped_column(Text)
    company_evidence: Mapped[str | None] = mapped_column(Text)
    #: The notice title exactly as it was judged, so the answer remains readable
    #: after a re-published notice changes its title.
    judged_title: Mapped[str | None] = mapped_column(Text)
    #: ``exact_core`` | ``semantic_core`` | ``adjacent`` | ``support_only`` |
    #: ``generic_only`` | ``no_evidence``. The rank is derived from the
    #: matched company line's role, not from the model's confidence.
    match_type: Mapped[str | None] = mapped_column(Text)
    evidence_rank: Mapped[int | None] = mapped_column(Integer)
    needs_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    matched_capability_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=sql_text("'{}'::uuid[]"),
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    rules_version: Mapped[str | None] = mapped_column(Text)
    judged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RequirementExperienceMatch(Base):
    """One tender requirement mapped against the company's own experience.

    ``coverage`` is ``covered``, ``partial``, ``new`` or ``unknown``. The
    company-side citation is copied from the stored capability or experience,
    never from the model's answer. The requirement-side citation is the
    requirement's own excerpt. A ``new`` row means the company has no
    experience of that kind of work, which is a prompt to look, not a pass.
    """

    __tablename__ = "requirement_experience_matches"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "requirement_id",
            name="uq_requirement_experience_matches_company_requirement",
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
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("requirements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coverage: Mapped[str] = mapped_column(Text, nullable=False)
    experience_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=sql_text("'{}'::uuid[]"),
    )
    capability_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=sql_text("'{}'::uuid[]"),
    )
    company_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_documents.id", ondelete="SET NULL"),
    )
    company_page_index: Mapped[int | None] = mapped_column(Integer)
    company_excerpt: Mapped[str | None] = mapped_column(Text)
    requirement_excerpt: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
