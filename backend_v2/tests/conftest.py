import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

SAMPLE_PAGES = [
    (
        "Section 3 - Eligibility Criteria. The bidder shall have an average annual "
        "turnover of at least INR 5 crore during the last three financial years. "
        "A bid security of INR 2,00,000 must be submitted with the bid."
    ),
    (
        "Section 5 - Submission of Bids. Bids must be submitted before 15 November 2026 "
        "at 17:00 IST through the online portal. Late bids shall be rejected."
    ),
    (
        "Section 8 - Penalties. Liquidated damages of 0.5 percent of the contract value "
        "per week of delay shall apply, subject to a maximum of 10 percent."
    ),
]


def make_pdf(pages: list[str]) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(
            pymupdf.Rect(50, 50, 545, 790),
            text,
            fontsize=11,
            fontname="helv",
        )
    data = document.tobytes()
    document.close()
    return data

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres@127.0.0.1:55432/bidkaro_v2_test",
)

# app.core.settings is imported by app.main; set the test URL before that.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# A developer's real .env key must not turn on the "AI configured" path: the
# tests that need a model inject a fake provider instead.
os.environ["GROQ_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
# Keep the auth tests fast and deterministic. Production defaults stay at 60s/5.
os.environ.setdefault("OTP_RESEND_COOLDOWN_SECONDS", "0")
os.environ.setdefault("OTP_MAX_SENDS_PER_HOUR", "100")
# No background worker or scheduler during the HTTP tests. The job tests build
# their own runner and call run_once/tick explicitly.
os.environ["JOBS_ENABLED"] = "false"
os.environ["SCHEDULER_ENABLED"] = "false"


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Apply migrations to the real test database once per session."""
    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "Could not migrate the test database.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    yield


@pytest.fixture(autouse=True)
def clean_database(migrated_database):
    from app.core.db import engine

    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE companies, users, otp_codes, audit_logs, "
                "company_documents, company_document_versions, "
                "company_document_pages, company_document_terms, jobs, runs, "
                "challenges, lane_leases, app_state, discovery_candidates, "
                "portals, tenders, tender_documents, document_pages, "
                "requirements, tender_covers, document_fetch_state, "
                "company_requirement_flags, company_tenders, evidence_matches, "
                "assessment_criteria, fit_assessments, checklist_tasks, "
                "tender_snapshots, changes, reminders, tender_observations, "
                "tender_classifications, company_tender_verdicts, "
                "company_relevance_feedback, company_capabilities, "
                "company_experiences, tender_similarity_judgments, "
                "requirement_experience_matches "
                "CASCADE"
            )
        )
    yield


class FakeEmail:
    configured = True

    def __init__(self) -> None:
        self.codes: list[tuple[str, str, str]] = []
        self.reminders: list[tuple[str, str, str]] = []

    def send_code(self, *, to: str, code: str, purpose: str, expires_minutes: int) -> None:
        self.codes.append((to, code, purpose))

    def send_reminder(self, *, to: str, subject: str, body: str) -> None:
        self.reminders.append((to, subject, body))


@pytest.fixture()
def fake_email() -> FakeEmail:
    return FakeEmail()


@pytest.fixture()
def sample_pdf() -> bytes:
    return make_pdf(SAMPLE_PAGES)


@pytest.fixture(autouse=True)
def clear_job_handlers():
    from app.services.job_registry import clear_handlers

    clear_handlers()
    yield
    clear_handlers()


@pytest.fixture(autouse=True)
def clear_browser_sessions():
    """A parked test session owns a thread; never leave one behind."""

    from app.discovery import sessions

    yield
    sessions.close_all()


@pytest.fixture()
def client(fake_email):
    from fastapi.testclient import TestClient

    from app.api.deps import get_email_service
    from app.main import app

    app.dependency_overrides[get_email_service] = lambda: fake_email
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
