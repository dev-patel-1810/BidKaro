from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.jobs import JobOut


class DiscoverySourceOut(BaseModel):
    url: str
    last_seen_at: datetime | None = None
    seen_count: int
    reason: str | None = None


class DiscoverySourceListOut(BaseModel):
    items: list[DiscoverySourceOut]
    next: None = None


class SweepStatusOut(BaseModel):
    last_sweep: JobOut | None = None
    #: What the last sweep actually did: its counts and its own closing note.
    last_summary: dict[str, Any] | None = None
    due: bool
    caps: dict[str, Any]


class DiscoveryStatusOut(BaseModel):
    enabled: bool
    last_run: JobOut | None = None
    last_completed_run_at: datetime | None = None
    due: bool
    tender_count: int
    source_count: int
    sweep: SweepStatusOut | None = None
    caps: dict[str, Any]


class DiscoveryRunStartOut(BaseModel):
    job: JobOut
    created: bool


class DiscoveryRunListOut(BaseModel):
    items: list[JobOut]
    next: None = None


class DocumentFetchStartOut(BaseModel):
    job: JobOut
    created: bool


class TenderRefreshStartOut(BaseModel):
    job: JobOut
    created: bool


class SimilarityBatchIn(BaseModel):
    """The notices to compare, or none to compare the first page."""

    tender_ids: list[UUID] = Field(default_factory=list, max_length=200)
