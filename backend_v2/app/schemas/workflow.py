from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ChangeReviewState = Literal["pending", "acknowledged", "dismissed"]
ReminderState = Literal["open", "sent", "dismissed"]


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tender_id: uuid.UUID
    captured_at: datetime
    content_hash: str
    fields: dict = {}
    requirement_count: int = 0
    requirement_fingerprints: list[dict] = []
    created_at: datetime


class SnapshotListOut(BaseModel):
    items: list[SnapshotOut]
    total: int
    next: str | None = None


class SnapshotAccepted(BaseModel):
    job_id: uuid.UUID
    state: str
    tender_id: uuid.UUID


class ChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tender_id: uuid.UUID
    from_snapshot_id: uuid.UUID | None = None
    to_snapshot_id: uuid.UUID
    change_type: str
    field_key: str
    old_value: str | None = None
    new_value: str | None = None
    detected_at: datetime
    review_state: ChangeReviewState
    reviewed_at: datetime | None = None
    reviewed_by: uuid.UUID | None = None
    source_url: str | None = None
    created_at: datetime


class ChangeListOut(BaseModel):
    items: list[ChangeOut]
    total: int
    next: str | None = None


class ChangeReviewRequest(BaseModel):
    review_state: Literal["pending", "acknowledged", "dismissed"]


class ReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tender_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    key: str
    type: str
    title: str
    body: str
    href: str
    due_at: datetime
    lead_days: int | None = None
    state: ReminderState
    sent_at: datetime | None = None
    acknowledged_at: datetime | None = None
    is_acknowledged: bool = False
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime


class ReminderListOut(BaseModel):
    items: list[ReminderOut]
    total: int
    next: str | None = None
    email_enabled: bool = False
    email_configured: bool = False
