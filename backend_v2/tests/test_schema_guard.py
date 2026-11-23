import logging
from pathlib import Path

import pytest
from sqlalchemy.exc import ProgrammingError

from app.core.schema import (
    current_revision,
    ensure_schema_current,
    head_revision,
    schema_status,
)


class _UnstampedSession:
    """A database that never ran a migration: no ``alembic_version`` table."""

    def __init__(self) -> None:
        self.rolled_back = False

    def execute(self, *_args, **_kwargs):
        raise ProgrammingError(
            "SELECT version_num FROM alembic_version",
            {},
            Exception('relation "alembic_version" does not exist'),
        )

    def rollback(self) -> None:
        self.rolled_back = True


def _status(
    revision: str | None, head: str | None = "0007_company_document_intel"
) -> dict:
    return {
        "revision": revision,
        "head": head,
        "up_to_date": head is not None and revision == head,
    }


def test_health_reports_the_schema_revision_it_is_running(client):
    body = client.get("/api/health").json()

    head = head_revision()
    assert head is not None
    assert body["status"] == "ok"
    assert body["schema"] == {"revision": head, "head": head, "up_to_date": True}


def test_a_database_behind_head_stops_the_startup():
    with pytest.raises(RuntimeError) as raised:
        ensure_schema_current(_status("0004_tender_cycle"))

    message = str(raised.value)
    assert "0004_tender_cycle" in message
    assert "0007_company_document_intel" in message
    assert "alembic upgrade head" in message


def test_a_stale_database_only_warns_when_the_guard_is_disabled(caplog):
    with caplog.at_level(logging.ERROR):
        ensure_schema_current(_status("0004_tender_cycle"), strict=False)

    assert "0004_tender_cycle" in caplog.text
    assert "alembic upgrade head" in caplog.text


def test_an_unstamped_database_reads_as_behind_not_as_fine():
    session = _UnstampedSession()

    assert current_revision(session) is None
    assert schema_status(session)["up_to_date"] is False
    assert session.rolled_back is True

    with pytest.raises(RuntimeError):
        ensure_schema_current(_status(None))


def test_an_unknown_head_is_reported_but_never_blocks_startup(caplog):
    with caplog.at_level(logging.WARNING):
        ensure_schema_current(_status("0004_tender_cycle", head=None))

    assert "no migration head" in caplog.text


def test_an_up_to_date_database_passes_silently(caplog):
    with caplog.at_level(logging.ERROR):
        ensure_schema_current(_status("0007_company_document_intel"))

    assert caplog.text == ""


def test_the_guard_never_imports_the_migration_tool():
    """The server may run where only the app requirements are installed.

    Reading the head is a file-system fact; importing alembic to learn it is
    what made a plain ``uvicorn app.main:app`` die with ``No module named
    'alembic'`` before it could serve a single request.
    """

    import app.core.schema as schema_module

    source = Path(schema_module.__file__).read_text(encoding="utf-8")
    assert "from alembic" not in source
    assert "import alembic" not in source
