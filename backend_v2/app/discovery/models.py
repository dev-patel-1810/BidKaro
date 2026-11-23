from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedTender(BaseModel):
    """Deterministic extraction result, with the verbatim excerpt for each field."""

    authority: str | None = None
    reference_number: str | None = None
    deadline: str | None = None  # ISO date (YYYY-MM-DD) when it could be parsed
    deadline_raw: str | None = None  # exact text the deadline was parsed from
    value_amount: float | None = None
    value_currency: str | None = None
    value_raw: str | None = None
    location: str | None = None
    evidence: dict[str, str] = Field(default_factory=dict)  # field -> excerpt
    unverified: list[str] = Field(default_factory=list)


class Relevance(BaseModel):
    """Deterministic profile-fit signal. Separate from eligibility and suitability."""

    score: float = 0.0
    tier: int = 7
    matched_terms: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    threshold: float = 0.0
    work_type_match: bool = False
    region_match: bool = False
