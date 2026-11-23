"""Small callbacks the base fires when a job ends or a tender starts bidding.

The base must never import the scraping module. A module registers a callback
here, and the base only knows the callback signature. With no module
installed, the callbacks are simply absent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

JobClosed = Callable[[str, uuid.UUID], None]
BiddingFetch = Callable[[uuid.UUID, uuid.UUID, uuid.UUID | None], None]

_closed: JobClosed | None = None
_bidding: BiddingFetch | None = None


def register_job_closed(callback: JobClosed) -> None:
    global _closed
    _closed = callback


def register_bidding_fetch(callback: BiddingFetch) -> None:
    global _bidding
    _bidding = callback


def notify_job_closed(kind: str, job_id: uuid.UUID) -> None:
    if _closed is not None:
        try:
            _closed(kind, job_id)
        except Exception:
            # A cleanup callback must never turn a finished job into a failure.
            pass


def notify_bidding(
    tender_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> None:
    if _bidding is not None:
        _bidding(tender_id, company_id, actor_user_id)


def clear_callbacks() -> None:
    global _closed, _bidding
    _closed = None
    _bidding = None
