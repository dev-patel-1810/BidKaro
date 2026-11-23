#!/usr/bin/env python
"""Send due BidKaro reminders through the configured Gmail SMTP account.

This is a cron-able job, not a background scheduler. It only emails companies
that explicitly enabled email reminders, only for reminders that are due, and
it records every attempt and error in Postgres.

Examples (run from the repository root, using the backend venv):

    .\\backend_v2\\.venv\\Scripts\\python.exe backend_v2\\scripts\\send-reminders.py
    .\\backend_v2\\.venv\\Scripts\\python.exe backend_v2\\scripts\\send-reminders.py --apply
    .\\backend_v2\\.venv\\Scripts\\python.exe backend_v2\\scripts\\send-reminders.py --apply --limit 50

If SMTP_APP_PASSWORD is missing, --apply exits with code 2 and sends nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Settings reads backend_v2/.env, not the repository root's file.
os.chdir(ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - optional convenience only
    pass

from sqlalchemy import text  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402
from app.services.email import EmailService  # noqa: E402
from app.services.reminders import ReminderService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually send email. Without this flag the job only reports what is due.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum reminders to consider in one run (default: 100).",
    )
    args = parser.parse_args()

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the CLI must fail clearly
        print(f"Postgres is not reachable: {exc}", file=sys.stderr)
        return 2

    email = EmailService()
    if args.apply and not email.configured:
        print(
            "SMTP_APP_PASSWORD (or SMTP_USERNAME) is not configured. "
            "No reminder emails were sent.",
            file=sys.stderr,
        )
        return 2

    with SessionLocal() as session:
        result = ReminderService(session, email=email).send_due(
            dry_run=not args.apply,
            limit=max(args.limit, 1),
            email=email,
        )

    mode = "SEND" if args.apply else "DRY RUN"
    print(
        f"[{mode}] considered={len(result['sent'])} failed={len(result['failed'])} "
        f"companies_skipped={len(result['skipped'])}"
    )
    for item in result["sent"]:
        print(
            f"  due {item['due_at']} -> {item['to']}: {item['subject']}"
        )
    for item in result["failed"]:
        print(f"  failed {item['reminder_id']}: {item['error']}", file=sys.stderr)
    for item in result["skipped"]:
        print(f"  skipped company {item['company_id']}: {item['reason']}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
