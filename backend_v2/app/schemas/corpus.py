from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["eligibility", "submission", "compliance", "form", "performance"]
Phase = Literal["pre_award", "post_award"]
MandatoryState = Literal["mandatory", "not_mandatory", "unknown"]


class TenderOut(BaseModel):
    id: uuid.UUID
    portal_id: uuid.UUID | None = None
    portal_tender_id: str | None = None
    reference: str | None = None
    authority: str | None = None
    organisation_chain: str | None = None
    work_title: str | None = None
    work_type: str | None = None
    location: str | None = None
    region: str | None = None
    closing_at: datetime | None = None
    published_at: datetime | None = None
    value_amount: Decimal | None = None
    currency: str
    source_type: str
    extracted_from: str
    source_url: str | None = None
    retrieved_at: datetime | None = None
    cycle_state: str = "active"
    termination_excerpt: str | None = None
    termination_observed_at: datetime | None = None
    unverified: list[str] = []
    supersedes_tender_id: uuid.UUID | None = None
    supersedes_state: str | None = None
    merged_into_tender_id: uuid.UUID | None = None
    document_count: int = 0
    requirement_count: int = 0
    requirement_summary: dict[str, int] = {}
    fetch_state: dict[str, Any] | None = None
    relevance: dict[str, Any] | None = None
    #: The cached closed-label answer ("yes"/"no"/"unsure") about this tender,
    #: or null when it has not been judged. It never changes eligibility.
    similarity: dict[str, Any] | None = None
    #: Whether this company has this tender saved / in bid preparation. Filled
    #: from the same company row the list already loaded, so a card can offer
    #: "remove from saved" without guessing.
    saved: bool = False
    bidding: bool = False
    created_at: datetime
    updated_at: datetime


class TenderListOut(BaseModel):
    items: list[TenderOut]
    total: int = 0
    page: int = 1
    pages: int = 1
    next: str | None = None
    #: False when the company has not declared any work (services, sectors or
    #: keywords). The list is then the whole corpus, not a profile match.
    profile_complete: bool = True


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tender_id: uuid.UUID
    filename: str
    sha256: str
    version: str
    document_key: str
    supersedes_document_id: uuid.UUID | None = None
    is_current: bool
    mime: str
    bytes: int
    page_count: int | None = None
    has_text_layer: bool
    ocr_page_count: int
    extraction_state: str
    extraction_error: str | None = None
    source_url: str | None = None
    fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    citation_counts: dict[str, int] = {}


class CoverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: str
    cover_no: int
    doc_type: str | None = None
    description: str | None = None


class FetchStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_requested_at: datetime | None = None
    last_attempt_at: datetime | None = None
    attempts: int
    expected_covers: int | None = None
    documents_present: int
    missing_covers: list[int] = []
    last_error: str | None = None
    updated_at: datetime


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    covers: list[CoverOut] = []
    fetch_state: FetchStateOut | None = None
    next: str | None = None


class DocumentArchiveOut(BaseModel):
    """What one uploaded zip produced: the PDFs attached and what was skipped."""

    documents: list[DocumentOut]
    skipped: list[str] = []


class PageIndexOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_index: int
    printed_label: str | None = None
    char_count: int
    is_ocr: bool


class PageOut(PageIndexOut):
    id: uuid.UUID
    document_id: uuid.UUID
    text: str


class RequirementDocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    version: str
    is_current: bool
    has_text_layer: bool
    source_url: str | None = None
    fetched_at: datetime | None = None


class RequirementOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    tender_id: uuid.UUID | None = None
    page_index: int
    printed_label: str | None = None
    clause_ref: str | None = None
    form_ref: str | None = None
    category: str
    phase: str
    title: str | None = None
    obligation: str | None = None
    deadline_text: str | None = None
    deadline_at: datetime | None = None
    consequence_text: str | None = None
    acceptance_criteria: str | None = None
    issuer: str | None = None
    signatory: str | None = None
    evidence_check: str | None = None
    excerpt: str
    is_navigation: bool
    state: str
    state_reason: str | None = None
    mandatory_state: str
    origin: str
    model: str | None = None
    prompt_version: str | None = None
    is_active: bool
    reviewed_by: uuid.UUID | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    document: RequirementDocumentOut | None = None
    flagged: bool = False
    flag_note: str | None = None


class RequirementListOut(BaseModel):
    items: list[RequirementOut]
    total: int
    next: str | None = None


class RequirementSummaryOut(BaseModel):
    verified: int
    needs_review: int
    accepted_ocr: int
    unverified: int
    rejected: int
    total: int


class RequirementCreateRequest(BaseModel):
    document_id: uuid.UUID
    page_index: int = Field(ge=1)
    excerpt: str = Field(min_length=20, max_length=5000)
    category: Category = "compliance"
    phase: Phase = "pre_award"
    title: str | None = Field(default=None, max_length=200)
    obligation: str | None = Field(default=None, max_length=2000)
    mandatory_state: MandatoryState = "unknown"
    clause_ref: str | None = Field(default=None, max_length=160)
    form_ref: str | None = Field(default=None, max_length=160)
    deadline_text: str | None = Field(default=None, max_length=500)
    consequence_text: str | None = Field(default=None, max_length=1000)
    acceptance_criteria: str | None = Field(default=None, max_length=1000)
    issuer: str | None = Field(default=None, max_length=200)
    signatory: str | None = Field(default=None, max_length=200)
    evidence_check: str | None = Field(default=None, max_length=500)


class RequirementPatchRequest(BaseModel):
    state: Literal["needs_review", "rejected", "accepted_ocr"]
    note: str | None = Field(default=None, max_length=1000)


class RequirementFlagRequest(BaseModel):
    ignored: bool | None = None
    note: str | None = Field(default=None, max_length=1000)


class ExtractionAccepted(BaseModel):
    job_id: uuid.UUID
    state: str
    document_id: uuid.UUID


class ManualTenderAccepted(BaseModel):
    tender: TenderOut
    created: bool


class CountsOut(BaseModel):
    counts: dict[str, Any]
