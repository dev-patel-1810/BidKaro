from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.job_types import JobResult

JobHandler = Callable[[Any], JobResult | None]

_handlers: dict[str, JobHandler] = {}


def register_handler(kind: str, handler: JobHandler) -> None:
    """The single seam the scraping module uses to declare a job kind.

    The base never imports a scraper. A module imports this function, registers
    its handler at import time, and the runner picks it up. With no handler
    registered a job fails with a readable reason instead of pretending to run.
    """

    _handlers[kind] = handler


def get_handlers() -> dict[str, JobHandler]:
    return dict(_handlers)


def clear_handlers() -> None:
    _handlers.clear()
