from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class JobResult:
    """What a handler returns when it finished normally."""

    state: str = "done"  # done | partial
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None


class JobParked(Exception):
    """Raised by ``context.park`` to unwind the handler without failing it."""

    def __init__(self, challenge_id) -> None:
        super().__init__("The job is waiting for a human.")
        self.challenge_id = challenge_id


class JobCancelled(Exception):
    """Raised when the user cancelled the job while its handler was running.

    A cancelled job must stop the browser work instead of finishing the walk
    or the fetch. The heartbeat checks the row, so every long loop that already
    asks for a heartbeat notices the cancellation at its next step.
    """
