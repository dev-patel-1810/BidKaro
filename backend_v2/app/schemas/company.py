from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

UnderstandingState = Literal["proposed", "confirmed", "rejected"]


class CompanyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    sectors: list[str] | None = None
    services: list[str] | None = None
    keywords: list[str] | None = None
    profile: dict | None = None
    reminder_opt_in: bool | None = None
    reminder_lead_days: list[int] | None = None

    @field_validator("sectors", "services", "keywords")
    @classmethod
    def clean_terms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        seen: list[str] = []
        for item in value:
            term = item.strip()
            if term and term.casefold() not in {existing.casefold() for existing in seen}:
                seen.append(term)
        return seen

    @field_validator("reminder_lead_days")
    @classmethod
    def valid_lead_days(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        days = sorted({int(day) for day in value if int(day) >= 0}, reverse=True)
        if not days:
            raise ValueError("Choose at least one reminder lead day.")
        return days


class CompanyDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_type: str
    title: str
    issuer: str | None = None
    signatory: str | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    financial_year: str | None = None
    project_value_amount: Decimal | None = None
    project_value_currency: str
    notes: str | None = None
    verification_status: str
    created_at: datetime
    updated_at: datetime
    sha256: str | None = None
    mime: str | None = None
    bytes: int | None = None
    extraction_state: str = "pending"
    extraction_error: str | None = None
    extracted_at: datetime | None = None
    term_count: int = 0
    understanding_state: str = "pending"
    understanding_error: str | None = None
    understanding_at: datetime | None = None
    capability_count: int = 0
    experience_count: int = 0


class CompanyDocumentPatch(BaseModel):
    document_type: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    issuer: str | None = None
    signatory: str | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    financial_year: str | None = None
    project_value_amount: Decimal | None = None
    project_value_currency: str | None = Field(default=None, min_length=3, max_length=3)
    notes: str | None = None


class CapabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    name: str
    description: str | None = None
    aliases: list[str] = []
    page_index: int
    printed_label: str | None = None
    excerpt: str
    quote_verified: bool
    is_ocr: bool
    state: str
    origin: str
    model: str | None = None
    prompt_version: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ExperienceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    title: str
    client: str | None = None
    description: str | None = None
    work_family: str | None = None
    value_amount: Decimal | None = None
    currency: str
    completed_on: date | None = None
    page_index: int
    printed_label: str | None = None
    excerpt: str
    quote_verified: bool
    is_ocr: bool
    state: str
    origin: str
    model: str | None = None
    prompt_version: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CapabilityPatch(BaseModel):
    state: UnderstandingState | None = None
    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    aliases: list[str] | None = None


class ExperiencePatch(BaseModel):
    state: UnderstandingState | None = None
    title: str | None = Field(default=None, min_length=2, max_length=200)
    client: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    work_family: str | None = Field(default=None, max_length=200)


class UnderstandingDocumentOut(BaseModel):
    document_id: uuid.UUID
    title: str
    understanding_state: str
    understanding_error: str | None = None
    understanding_at: datetime | None = None
    capability_count: int = 0
    experience_count: int = 0


class UnderstandingCountsOut(BaseModel):
    capabilities: int = 0
    experiences: int = 0
    confirmed_capabilities: int = 0
    confirmed_experiences: int = 0


class CompanyUnderstandingOut(BaseModel):
    documents: list[UnderstandingDocumentOut] = []
    capabilities: list[CapabilityOut] = []
    experiences: list[ExperienceOut] = []
    counts: UnderstandingCountsOut
    rules_version: str


BriefRole = Literal["core", "adjacent", "support", "generic"]


class BriefTerm(BaseModel):
    model_config = ConfigDict(extra="allow")

    term: str = Field(min_length=1, max_length=200)
    #: The section key is the role a human chose. The default is deliberately
    #: unset so a term sent under ``core_terms`` is not silently saved as
    #: adjacent because the client omitted a redundant field.
    role: BriefRole | None = None
    source: str | None = None
    source_id: str | None = None
    document_id: str | None = None
    evidence: str | None = Field(default=None, max_length=1200)
    page_index: int | None = None
    printed_label: str | None = None
    state: str | None = None


class BriefProject(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str = Field(min_length=1, max_length=300)
    client: str | None = None
    work_family: str | None = None
    description: str | None = None
    document_id: str | None = None
    source_id: str | None = None
    page_index: int | None = None
    printed_label: str | None = None
    evidence: str | None = Field(default=None, max_length=1200)


class CompanyBriefPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    summary: str = Field(default="", max_length=2000)
    core_terms: list[BriefTerm] = []
    adjacent_terms: list[BriefTerm] = []
    support_terms: list[BriefTerm] = []
    generic_terms: list[BriefTerm] = []
    projects: list[BriefProject] = []
    certifications: list[dict] = []
    rules_version: str | None = None


class CompanyBriefOut(BaseModel):
    id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    version: int | None = None
    is_current: bool = False
    source_kind: str = "derived"
    source_document_id: uuid.UUID | None = None
    payload: dict
    prompt_version: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CompanyBriefSaveRequest(BaseModel):
    payload: CompanyBriefPayload


class CompanyBriefVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    is_current: bool
    source_kind: str
    source_document_id: uuid.UUID | None = None
    summary: str | None = None
    counts: dict[str, int] = {}
    created_by: uuid.UUID | None = None
    created_at: datetime | None = None
