"""Run the agentic pipeline for real, against a live portal, through the API.

This boots the actual FastAPI app, creates a company with a profile, starts
an agent run through ``POST /api/agent/runs``, waits for it, and then reads
the tenders back off ``GET /api/discovery/tenders`` - the same page the user
sees. Nothing is mocked except the database, which runs on mongomock so no
MongoDB is needed.

    python scripts/agent-live.py --url "<notice url>" --mode browser
    python scripts/agent-live.py --url "<directory url>" --mode agent --kind portal
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import mongomock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Database, get_database  # noqa: E402
from app.core.dependencies import get_email_service, get_storage  # noqa: E402
from app.main import app  # noqa: E402
from app.services.storage import StorageService  # noqa: E402

PROFILE = {
    "sectors": ["Infrastructure", "Highways"],
    "products_services": ["Road construction", "Road resurfacing"],
    "regions": ["Maharashtra"],
    "preferences": {"keywords": ["highway"], "sectors": [], "regions": []},
}


class NoEmail:
    """Stands in for Gmail so the run needs no live SMTP.

    It keeps the code so the account can be verified the same way the test
    suite does it.
    """

    configured = True

    def __init__(self) -> None:
        self.codes: dict[tuple[str, str], str] = {}

    def send_otp(self, *, to, code, purpose, expires_minutes):
        self.codes[(to.lower(), purpose)] = code

    def send_reminder(self, **kwargs):
        return None

    def code_for(self, email, purpose):
        return self.codes[(email.lower(), purpose)]


def build_client() -> tuple[TestClient, NoEmail]:
    database = Database(
        "mongodb://localhost:27017",
        "bidkaro_live",
        client=mongomock.MongoClient(tz_aware=True),
    )
    database.init_indexes()
    email = NoEmail()
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_storage] = lambda: StorageService(
        Path("/tmp/bidkaro-live-storage"), 25
    )
    app.dependency_overrides[get_email_service] = lambda: email
    return TestClient(app), email


def sign_up(client: TestClient, email: NoEmail) -> dict[str, str]:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "live-run@example.com",
            "password": "StrongPass123",
            "full_name": "Live Runner",
            "company_name": "Live Road Contractor",
        },
    )
    if registered.status_code != 201:
        print(registered.text, file=sys.stderr)
        raise SystemExit(1)
    verified = client.post(
        "/api/auth/verify-otp",
        json={
            "email": "live-run@example.com",
            "purpose": "verify_email",
            "code": email.code_for("live-run@example.com", "verify_email"),
        },
    )
    if verified.status_code != 200:
        print(verified.text, file=sys.stderr)
        raise SystemExit(1)
    # Verifying the account hands back the access token, so no sign-in step.
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", default="browser", choices=["browser", "agent"])
    parser.add_argument("--kind", default="notice", choices=["notice", "portal"])
    parser.add_argument("--wait", type=int, default=900)
    parser.add_argument("--answer", help="Answer a challenge with this text.")
    args = parser.parse_args()

    client, email = build_client()
    headers = sign_up(client, email)

    profile = client.patch("/api/companies/me", headers=headers, json=PROFILE)
    print(f"profile saved: {profile.status_code}")

    status = client.get("/api/agent/status", headers=headers)
    print(f"agent status: {status.json()}")

    started = client.post(
        "/api/agent/runs",
        headers=headers,
        json={"url": args.url, "mode": args.mode, "kind": args.kind},
    )
    print(f"run started: {started.status_code}")
    if started.status_code != 200:
        print(started.text, file=sys.stderr)
        return 1
    run = started.json()
    run_id = run["id"]
    print(f"  id={run_id} mode={run['mode']} kind={run['kind']} status={run['status']}")
    print(f"  session keys: {run['keys']}")

    deadline = time.monotonic() + args.wait
    last = ""
    while time.monotonic() < deadline:
        refreshed = client.get(
            f"/api/agent/runs/{run_id}", headers=headers, params={"refresh": True}
        )
        if refreshed.status_code != 200:
            print(refreshed.text, file=sys.stderr)
            return 1
        run = refreshed.json()
        if run["status"] != last:
            last = run["status"]
            print(f"  status -> {last}")
        if run["status"] in {"completed", "failed", "cancelled"}:
            break
        if run["status"] == "awaiting_human":
            print("\n>>> A HUMAN IS NEEDED. The run is paused on a challenge.")
            print(f"    message: {run['challenge']['message']}")
            print(f"    capture: {run['challenge']['capture_url']}")
            if not args.answer:
                print("    (pass --answer to answer it, or answer it in the UI)")
                return 2
            answered = client.post(
                f"/api/agent/runs/{run_id}/challenge",
                headers=headers,
                json={"answer": args.answer},
            )
            print(f"    answered: {answered.status_code}")
            last = "running"
            continue
        time.sleep(5)

    print(f"\nfinal status: {run['status']}")
    if run.get("error"):
        print(f"error: {run['error']}")
    if run.get("walk"):
        print(f"walk: {run['walk']}")
    if run.get("fields"):
        print("\n--- fields read ---")
        for field in run["fields"]:
            mark = "on the page" if field["verified"] else "UNCONFIRMED"
            print(f"  {field['label']}: {field['value']}   [{mark}]")
    if run.get("notices"):
        print(f"\n--- notices read: {len(run['notices'])} ---")
        for notice in run["notices"]:
            print(f"  {notice['title'][:90]}")
            print(f"    ref={notice.get('reference_number')} tender_id={notice.get('tender_id')}")
            print(f"    closing={notice.get('closing_date')} url={notice.get('notice_url')}")

    discovered = client.get("/api/discovery/tenders", headers=headers)
    items = discovered.json()["items"]
    print(f"\n--- Discover tenders: {len(items)} listed ---")
    for item in items:
        extracted = item["extracted"]
        print(f"  {item['title'][:90]}")
        print(f"    authority : {extracted.get('authority')}")
        print(f"    reference : {extracted.get('reference_number')}")
        print(f"    deadline  : {extracted.get('deadline')}")
        print(f"    value     : {extracted.get('value_amount')} {extracted.get('value_currency')}")
        print(f"    source    : {item['source']} / {item['source_type']} / {item['extracted_from']}")
        print(f"    url       : {item['source_url']}")
        print(f"    relevance : {item['relevance']['score']} {item['relevance']['reasons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
