"""Run the TinyFish agent against a URL and show what it did.

This is a working tool, not a demo. It starts a real agent run, prints each
step the agent takes, keeps the live browser link so the run can be watched,
and writes the final answer to a JSON file.

    python scripts/agent-explore.py --url https://example.com
    python scripts/agent-explore.py --goal-file goal.txt --out run.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.integrations.tinyfish_agent import (  # noqa: E402
    TinyFishAgentClient,
    TinyFishAgentError,
)

#: The shape we ask the agent to answer in. Every field is checked against the
#: notice text afterwards, so a wrong answer cannot pass as fact.
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "human_required", "not_found", "partial"],
        },
        "challenge": {"type": "string"},
        "page_text": {"type": "string"},
        "fields": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "authority": {"type": "string"},
                "organisation_chain": {"type": "string"},
                "reference_number": {"type": "string"},
                "tender_id": {"type": "string"},
                "closing_date": {"type": "string"},
                "publishing_date": {"type": "string"},
                "opening_date": {"type": "string"},
                "value": {"type": "string"},
                "location": {"type": "string"},
                "bid_validity": {"type": "string"},
                "eligibility": {"type": "string"},
                "documents_required": {"type": "string"},
                "earnest_money": {"type": "string"},
                "notice_url": {"type": "string"},
            },
        },
    },
    "required": ["status", "fields"],
}


def read_key() -> str:
    for candidate in (BACKEND.parent / ".env", BACKEND / ".env"):
        if not candidate.exists():
            continue
        match = re.search(r"^TINYFISH_API_KEY=(.*)$", candidate.read_text(), re.M)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            if value:
                return value
    print("TINYFISH_API_KEY is not set in .env", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--goal")
    parser.add_argument("--goal-file")
    parser.add_argument("--out", help="Write the final answer to this JSON file.")
    parser.add_argument("--profile", default="lite", choices=["lite", "stealth"])
    # Not every account is enabled for custom step limits, so these are opt-in.
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--wait", type=int, default=600, help="Seconds to wait for the answer.")
    args = parser.parse_args()

    goal = args.goal
    if args.goal_file:
        goal = Path(args.goal_file).read_text(encoding="utf-8")
    if not goal:
        parser.error("Give a goal with --goal or --goal-file.")

    client = TinyFishAgentClient(read_key())
    limits: dict = {}
    if args.max_steps:
        limits["max_steps"] = args.max_steps
    if args.max_seconds:
        limits["max_duration_seconds"] = args.max_seconds
    try:
        run_id = client.start_run(
            args.url,
            goal,
            browser_profile=args.profile,
            output_schema=OUTPUT_SCHEMA,
            capture_screenshots=True,
            capture_html=True,
            api_integration="bidkaro-explore",
            **limits,
        )
    except TinyFishAgentError as exc:
        print(f"Could not start the run: {exc}", file=sys.stderr)
        return 1

    print(f"run started: {run_id}")
    print(f"watch it live: https://agent.tinyfish.ai/v1/runs/{run_id}")

    deadline = time.monotonic() + args.wait
    seen_steps = 0
    streaming_url = None
    run = None
    while time.monotonic() < deadline:
        try:
            run = client.get_run(run_id)
        except TinyFishAgentError as exc:
            print(f"could not read the run: {exc}", file=sys.stderr)
            return 1
        if run.streaming_url and not streaming_url:
            streaming_url = run.streaming_url
            print(f"live browser: {streaming_url}")
        for step in run.steps[seen_steps:]:
            print(f"  - [{step.status or '?'}] {step.action}")
        seen_steps = len(run.steps)
        if run.finished:
            break
        time.sleep(3)

    if run is None:
        print("no answer was produced", file=sys.stderr)
        return 1

    print(f"\nstatus: {run.status}  steps: {run.num_of_steps}")
    if run.error:
        print(f"error: {json.dumps(run.error, ensure_ascii=False)[:800]}")
    if streaming_url:
        print(f"live browser (while running): {streaming_url}")
    if run.video_url:
        print(f"recording: {run.video_url}")

    payload = run.result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    print("\n--- answer ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:12000])

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "url": args.url,
                    "status": run.status,
                    "result": payload,
                    "steps": [
                        {"action": s.action, "status": s.status, "screenshot": s.screenshot}
                        for s in run.steps
                    ],
                    "video_url": run.video_url,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
