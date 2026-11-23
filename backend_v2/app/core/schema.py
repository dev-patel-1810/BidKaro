"""Schema revision guard.

A server running against a database that is behind the migration head does not
fail at startup; it fails one request at a time with an obscure
``UndefinedTable`` (``relation "company_relevance_feedback" does not exist``)
deep inside a repository. That is what happened when ``0005_corpus_intelligence``
landed and a dev server kept running against a database stamped ``0004``.

The database revision is a fact about the deployment, so it is read and
compared with the migration head once at startup, reported by ``/api/health``
and, when the schema is behind, refused loudly with the command that fixes it.
The check never invents a revision: a database that was never stamped is
reported as ``None``, not as "fine".
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# app/core/schema.py -> app/core -> app -> backend_v2/migrations
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

MIGRATE_HINT = (
    "Run the migrations before starting the server:\n"
    "  cd backend_v2\n"
    '  $env:DATABASE_URL="<the same DATABASE_URL the server uses>"\n'
    "  .\\.venv\\Scripts\\python.exe -m alembic upgrade head"
)


def _assigned_literal(node: ast.stmt) -> tuple[str, object] | None:
    """``("revision", "0005_...")`` for a module-level literal assignment.

    Alembic's own loader is not imported here: the head is read from the files
    this checkout carries, so a runtime that lacks the migration tool (and a
    deployment that installs only the app requirements) can still answer the
    one question the guard asks.
    """

    if isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        value = node.value
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names = [node.target.id]
        value = node.value
    else:
        return None
    if not names or not isinstance(value, ast.Constant):
        return None
    return names[0], value.value


def head_revision() -> str | None:
    """The head revision of the migrations in this checkout.

    The head is the revision no other migration names as its parent. A branch
    (two heads) is ambiguous, so it is reported as unknown rather than guessed.
    ``None`` when the checkout has no readable migration directory: the guard
    must not turn a packaging mistake into a crash that looks like a schema
    problem.
    """

    versions = MIGRATIONS_DIR / "versions"
    revisions: set[str] = set()
    parents: set[str] = set()
    try:
        files = sorted(versions.glob("*.py"))
        for path in files:
            module = ast.parse(path.read_text(encoding="utf-8"))
            for node in module.body:
                assigned = _assigned_literal(node)
                if assigned is None:
                    continue
                name, value = assigned
                if name == "revision" and isinstance(value, str):
                    revisions.add(value)
                elif name == "down_revision":
                    if isinstance(value, str):
                        parents.add(value)
                    elif isinstance(value, (tuple, list)):
                        parents.update(v for v in value if isinstance(v, str))
    except (OSError, SyntaxError):
        logger.warning("Could not read the migration head from %s", versions)
        return None

    heads = sorted(revisions - parents)
    if len(heads) != 1:
        logger.warning(
            "Expected one migration head in %s, found %s: %s",
            versions,
            len(heads),
            heads or "none",
        )
        return None
    return heads[0]


def current_revision(session: Session) -> str | None:
    """The revision the database is stamped with, or ``None`` when unstamped.

    A missing ``alembic_version`` table means the database was never migrated
    (or ``DATABASE_URL`` points at another database): that is a stale schema,
    so it reads as ``None``. A connection failure is not caught here; the
    caller decides whether an unreachable database is fatal.
    """

    try:
        return session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except ProgrammingError:
        session.rollback()
        return None


def schema_status(session: Session) -> dict:
    """``{"revision", "head", "up_to_date"}`` for the given session."""

    head = head_revision()
    revision = current_revision(session)
    return {
        "revision": revision,
        "head": head,
        "up_to_date": head is not None and revision == head,
    }


def ensure_schema_current(status: dict, *, strict: bool = True) -> None:
    """Raise (or warn) when the database is behind the migration head.

    ``strict=False`` is for a deployment that deliberately boots a stale
    database (``SCHEMA_CHECK=false``); it still logs, because a silent stale
    schema is how a 500 looks like an application bug.
    """

    if status["up_to_date"]:
        return
    if status["head"] is None:
        logger.warning(
            "Could not verify the database schema: no migration head was readable in this checkout."
        )
        return
    message = (
        f"The database schema is stamped {status['revision'] or 'none'}, "
        f"but this code expects {status['head']}. "
        f"{MIGRATE_HINT}"
    )
    if strict:
        raise RuntimeError(message)
    logger.error("%s", message)
