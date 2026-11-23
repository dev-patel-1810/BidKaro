from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GateVerdict = Literal["pass", "fail", "unknown"]
EvidenceState = Literal[
    "none", "suggested", "covered", "missing", "expired", "contradicted"
]
ChecklistStatus = Literal[
    "open", "in_progress", "done", "blocked", "not_applicable"
]
ChecklistPhase = Literal["pre_award", "post_award"]


class EvidenceCandidateOut(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    title: str
    document_type: str
    issuer: str | None = None
    expiry_date: date | None = None
    score: float
    matched_terms: list[str] = []
    reasons: list[str] = []
    valid: bool = True
    validity_note: str | None = None


class RequirementEvidenceOut(BaseModel):
    requirement_id: uuid.UUID
    status: EvidenceState = "none"
    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    document_title: str | None = None
    note: str | None = None
    confirmed_at: datetime | None = None
    validity_note: str | None = None
    candidates: list[EvidenceCandidateOut] = []


class EvidenceDecisionRequest(BaseModel):
    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    status: Literal["covered", "contradicted", "missing"]
    note: str | None = Field(default=None, max_length=1000)


class EligibilityItemOut(BaseModel):
    requirement_id: uuid.UUID
    title: str
    category: str
    mandatory_state: str
    excerpt: str
    page_index: int
    printed_label: str | None = None
    clause_ref: str | None = None
    form_ref: str | None = None
    document_id: uuid.UUID
    document_title: str | None = None
    citation_state: str
    ocr_confirmed: bool = False
    gate: GateVerdict = "unknown"
    evidence_status: EvidenceState = "none"
    evidence_document_id: uuid.UUID | None = None
    evidence_document_version_id: uuid.UUID | None = None
    evidence_document_title: str | None = None
    profile_hints: list[str] = []
    reason: str


class EligibilitySummaryOut(BaseModel):
    status: GateVerdict = "unknown"
    pass_count: int = 0
    fail_count: int = 0
    unknown_count: int = 0
    items: list[EligibilityItemOut] = []
    message: str


class ScoreDimensionOut(BaseModel):
    key: str
    label: str
    weight: int
    score: float
    contribution: float
    reason: str


class FitScoreOut(BaseModel):
    value: float = 0
    band: Literal["strong", "moderate", "weak", "insufficient"] = "insufficient"
    dimensions: list[ScoreDimensionOut] = []
    explanation: str
    gating_status: GateVerdict = "unknown"
    generated_at: datetime


class AssessmentCountsOut(BaseModel):
    total: int = 0
    verified: int = 0
    accepted_ocr: int = 0
    mandatory_gating: int = 0
    checklist_total: int = 0
    checklist_open: int = 0


class AssessmentOut(BaseModel):
    tender_id: uuid.UUID
    score: FitScoreOut
    eligibility: EligibilitySummaryOut
    counts: AssessmentCountsOut
    generated_at: datetime


class ChecklistTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tender_id: uuid.UUID
    requirement_id: uuid.UUID | None = None
    phase: ChecklistPhase
    title: str
    category: str | None = None
    acceptance_criteria: str | None = None
    issuer: str | None = None
    signatory: str | None = None
    evidence_check: str | None = None
    due_at: datetime | None = None
    trigger: str | None = None
    consequence: str | None = None
    document_id: uuid.UUID | None = None
    page_index: int | None = None
    clause_ref: str | None = None
    excerpt: str | None = None
    source_url: str | None = None
    status: ChecklistStatus
    owner: str | None = None
    created_at: datetime
    updated_at: datetime


class ChecklistTaskListOut(BaseModel):
    items: list[ChecklistTaskOut]
    total: int


class ChecklistGenerateOut(BaseModel):
    created: int = 0
    skipped: int = 0
    total: int = 0
    items: list[ChecklistTaskOut] = []
    message: str


class ChecklistTaskUpdate(BaseModel):
    status: ChecklistStatus | None = None
    owner: str | None = Field(default=None, max_length=120)
    due_at: datetime | None = None


class ChecklistTaskCreate(BaseModel):
    tender_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=80)
    phase: ChecklistPhase = "pre_award"
    requirement_id: uuid.UUID | None = None
    acceptance_criteria: str | None = Field(default=None, max_length=2000)
    issuer: str | None = Field(default=None, max_length=200)
    signatory: str | None = Field(default=None, max_length=200)
    evidence_check: str | None = Field(default=None, max_length=1000)
    due_at: datetime | None = None
    trigger: str | None = Field(default=None, max_length=1000)
    consequence: str | None = Field(default=None, max_length=1000)
    document_id: uuid.UUID | None = None
    page_index: int | None = Field(default=None, ge=1)
    clause_ref: str | None = Field(default=None, max_length=160)
    excerpt: str | None = Field(default=None, max_length=5000)
    source_url: str | None = Field(default=None, max_length=2000)


class LegalObligationOut(BaseModel):
    requirement_id: uuid.UUID
    title: str
    category: str
    phase: ChecklistPhase
    obligation: str
    excerpt: str
    page_index: int
    printed_label: str | None = None
    clause_ref: str | None = None
    form_ref: str | None = None
    document_id: uuid.UUID
    document_title: str | None = None
    deadline_text: str | None = None
    consequence_text: str | None = None
    state: str
    source_url: str | None = None
    retrieved_at: datetime | None = None


class LegalObligationListOut(BaseModel):
    items: list[LegalObligationOut]
    total: int
    disclaimer: str
