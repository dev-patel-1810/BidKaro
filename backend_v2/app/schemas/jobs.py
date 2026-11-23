from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChallengeOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    kind: str
    image_url: str | None = None
    context: dict[str, Any] = {}
    state: str
    opened_at: datetime
    expires_at: datetime
    answered_at: datetime | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID | None = None
    kind: str
    lane: str | None = None
    state: str
    priority: int
    payload: dict[str, Any] = {}
    attempts: int
    max_attempts: int
    last_error: str | None = None
    run_after: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    challenge: ChallengeOut | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    kind: str
    status: str
    metrics: dict[str, Any] = {}
    notes: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class ChallengeAnswerIn(BaseModel):
    """The characters the human read from the picture.

    The field is bounded and never echoed: it is typed into the portal's form
    and dropped, and no schema, column or log line keeps it.
    """

    answer: str = Field(min_length=1, max_length=16)


class ChallengeAnswerOut(BaseModel):
    accepted: bool
    job: JobOut
