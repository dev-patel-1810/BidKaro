# BidKaro v2 backend

FastAPI + Postgres. This tree is the rebuild described in `../docs/`; it does
not import or depend on the old MongoDB backend.

## Stages A-F, I, J and K (current)

- FastAPI app, typed settings, Postgres session, health endpoint.
- Hand-written Alembic migration transcribing `docs/SCHEMA-SPEC.md` with the
  2026-10-26 decisions folded in.
- Identity, auth/OTP, company profile and the evidence library.
- Durable jobs, `lane_leases`, scheduler catch-up and the visible-browser
  challenge signal.
- Tender documents: upload-first PDFs, PyMuPDF page text/labels, bounded
  Tesseract OCR fallback, SHA-256 dedupe, `document_key` version lifecycle.
- Cited requirements: AI extraction, per-item validation, deterministic
  verbatim/page gate, OCR `needs_review`, human review/creation, per-company
  flags, navigation filtering, and `POST /api/tenders` as the manual upload
  fallback.
- Deterministic assessment: separate mandatory eligibility gate and soft score,
  human-confirmed evidence pinned to a document version, tender-specific
  pre-award checklist and cited post-award obligations, and manual tasks.
- Workflow: global tender snapshots with deterministic field/requirement diffs,
  company-scoped reminders that reopen on a moved due date, authenticated ICS
  export, and proposed-then-human-confirmed corrigendum lineage.
- Discovery (Stage I): a deterministic CPPP portal walk behind the job queue,
  local Playwright adapter (optional dependency), global upsert with
  carry-forward, stable tender locators, rejection rows in
  `discovery_candidates`, and per-company listing relevance computed at read
  time. The base installs and tests without Playwright; the real walk needs it.
- Document fetch (Stage J): a global `fetch_documents` job opens the notice
  through its locator in one visible browser session, attaches PDFs/zip
  contents to the tender, stores the portal's cover table, and names missing
  covers without treating them as failure. A captcha is shown to the human in
  the headed browser; the job parks as `needs_you`, the human answers the
  portal's own form, and the download is picked up on resume. The software
  never receives or stores the answer, and a second captcha is reported rather
  than looped on.
- Schedule and recheck (Stage K): the scheduler catches up on startup when the
  last completed global run is older than 24 hours, a manual discovery run is
  the same idempotent `scrape_portal` path, and
  `POST /api/tenders/{id}/refresh` queues one `refresh_tender` browser job that
  re-reads a known notice by locator, updates the same row and captures a
  snapshot, or stores a new row and proposes corrigendum lineage.
- No MongoDB, no Qdrant. Discovery routes and the `scrape_portal`,
  `fetch_documents` and `refresh_tender` handlers are registered by
  `app/discovery/`; set `FEATURES_DISCOVERY=false` to hide them.
- The `../frontend/` app is adapted to this API: discovery, documents/covers,
  requirements/evidence, assessment, changes, reminders and the global checklist
  use the v2 field names directly. `cd ../frontend; npm test` (22 tests) and
  `npm run build` are green.

## Local setup

```powershell
cd backend_v2
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

The default `.env.example` points at a dedicated local cluster on port 55432.
`scripts/init-db.ps1` creates it with trust auth (the same pattern as the old
`start-mongo.ps1`); if you prefer your existing Postgres, set `DATABASE_URL`
to it and create `bidkaro_v2` and `bidkaro_v2_test` yourself.

```powershell
.\scripts\init-db.ps1
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Run the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Health: `GET http://localhost:8000/api/health`

Tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use a real Postgres database (`TEST_DATABASE_URL`, default
`bidkaro_v2_test`); SQL is never mocked.

### Real portal walk

The deterministic walk is tested with a fake page. To drive a real browser:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-discovery.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

`BROWSER_HEADLESS=true` is the safe server default. For a captcha handoff the
browser window must be visible; Stage J parks the job as `needs_you` and picks
the downloaded file up when the human has answered the portal's own form.

Reminder email is a cron-able script, not an in-process scheduler:

```powershell
.\.venv\Scripts\python.exe scripts\send-reminders.py            # dry run
.\.venv\Scripts\python.exe scripts\send-reminders.py --apply    # send
```

Live sending needs `SMTP_USERNAME` and `SMTP_APP_PASSWORD`; without them
`--apply` exits `2` and sends nothing.

## Decisions folded into the first schema

- Global corpus; per-company judgement in `company_tenders`, `evidence_matches`,
  `fit_assessments`, `checklist_tasks`, `reminders`.
- `tenders.dedupe_key` has no dates: `portal + normalised reference + title +
  organisation`; portal identity is the real key.
- `merged_into_tender_id` for an aggregator row later superseded by a portal row.
- Manual tenders: nullable `portal_id`, `source_type='manual_upload'`.
- `discovery_candidates` stores source pages and rejected candidates, not just counts.
- `requirements` carries citation, interpretation, deadlines, consequence,
  acceptance criteria, model/prompt provenance, `mandatory_state`, `is_navigation`
  and `is_active`; excerpt uniqueness is per page.
- `checklist_tasks` copies the citation at generation time and is unique per
  `(company, tender, requirement, phase)`.
- `assessment_criteria` stores per-criterion verdicts; `fit_assessments` stores
  the aggregate gate and score.
- `evidence_matches` points at a `company_document_version`, not the parent.
- `changes` has review state and source URL; `reminders` has acknowledgement,
  lead days and send errors; `companies` has reminder opt-in and lead days.
- `tender_documents` has `document_key`, `supersedes_document_id`,
  `extraction_state` and `extraction_error`.
- `lane_leases` is the DB-backed single browser lane.
- `challenges` has no answer column: the human answers in the portal.
- `jobs` is canonical; `/api/discovery/runs/{id}` is a thin alias for a
  `kind='scrape_portal'` job.
- `tender_locators` stores portal + portal tender id, not the session-bound
  notice address, so a fetch days later can find the same notice.
