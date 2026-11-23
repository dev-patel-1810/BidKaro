# Coding Guide — how to write code in this repo

**Read this at the start of every session.** It is short on purpose. It tells you *how*; the design contract lives in [`SYSTEM-BLUEPRINT.md`](./SYSTEM-BLUEPRINT.md) and is **not** repeated here.

| Doc | Answers |
|---|---|
| `AGENTS.md` | where am I, what are the commands |
| `docs/SYSTEM-BLUEPRINT.md` | **what** we build and **why** (the contract) |
| `docs/CODING-GUIDE.md` | **how** to write it (this file) |

If this guide and the blueprint disagree, **the blueprint wins** — fix this guide in the same commit.

---

## 1. The eight rules you must not break

These are implementation rules. The product rules (no fabricated citations, gate vs score, never solve a captcha) are in **BP Part 2** and are non-negotiable too.

1. **Layers are strict.** `routes → services → repositories`. A route never touches SQL; a repository never contains business decisions; a service never imports FastAPI.
2. **Every multi-step write is one transaction.** Attach document → pages → requirements → checklist either all lands or none does. If you write two things that must agree, they share a transaction.
3. **Deterministic logic takes plain data and returns plain data.** Relevance, citation gate, scoring, diffing, cover gaps — no DB, no HTTP, no clock except as a passed-in argument. That is what makes them testable in milliseconds.
4. **The database is the only source of truth.** No in-memory state that cannot be rebuilt. No queue, lock, or pointer that lives only in a process.
5. **Time is UTC; date-only deadlines are anchored at noon UTC** (BP §16.5) so the IST calendar day survives.
6. **Money is integer minor units or `Decimal`. Never `float`.**
7. **Nothing fails silently.** If something is missing, partial, unverified, or skipped, it is stored as a state and shown to the user (BP Part 9).
8. **Reads are company-scoped.** Every query for judgement data filters by `company_id`. Public corpus tables are not company-scoped (BP §6.1) — know which kind you are touching.

---

## 2. Stack

| Concern | Choice | Note |
|---|---|---|
| API | FastAPI | one worker; the browser lane needs a single process when Qdrant becomes embedded |
| DB access | SQLAlchemy 2.x + Alembic | migrations always; no `create_all()` in app paths |
| Driver | `psycopg` (sync) | pick `asyncpg` only if the whole layer goes async — do not mix |
| Validation | Pydantic v2 | schemas in `schemas/`, never in `models/` |
| PDF | PyMuPDF | text layer first, OCR as bounded fallback |
| OCR | `pytesseract` + Tesseract | English, 40 pages max, 45 s timeout |
| AI | Groq (`openai/gpt-oss-120b`) | output is a **proposal**; the gate decides |
| Email | Gmail SMTP | codes are hashed, single-use, expiring |
| Frontend | React + Vite, **JavaScript** | not TypeScript — decided |
| Tests | pytest; Vitest + Testing Library | ephemeral Postgres, not mocks |

No Playwright, no Qdrant, no browser driver in the base requirements until their stages arrive (BP §17.2).

---

## 3. Repo layout

```
backend/
  app/
    main.py            app wiring, lifespan, router registration
    core/              settings, db pool + session, security, error types
    models/            SQLAlchemy models, one module per aggregate
    schemas/           Pydantic in/out
    api/routes/        thin HTTP endpoints
    services/          rules + orchestration (pure where possible)
    repositories/      SQL only
    jobs/              worker loop, registry, job kinds
    integrations/      groq, smtp  (later: browser, portal)
  migrations/          alembic
  tests/
    test_rules_*       pure functions, no DB
    test_repo_*        SQL against ephemeral Postgres
    test_api_*         HTTP contracts
    test_jobs_*        state transitions
frontend/              React + Vite
docs/
scripts/               seeding, reminders, demo
storage/               content-addressed documents (git-ignored)
```

Rule of thumb: **if a file needs both SQL and a business decision, it is in the wrong folder.**

---

## 4. The three layers

### Routes (`api/routes/`)
- Parse and validate input, call one service, shape the response.
- No SQL. No rules. No `if` about eligibility, deadlines or scoring.
- Every handler that touches judgement data resolves `company_id` from the authenticated user and passes it down. Never trust a `company_id` from the request body.
- Return explicit states, not bare 500s.

### Services (`services/`)
- Two kinds, and keep them apart:
  - **Pure rules** (`*_rules.py`): take data, return data. No imports from `repositories`, `sqlalchemy`, `fastapi`, or `datetime.now()`.
  - **Orchestration** (`*_service.py`): coordinates repositories, owns the transaction, emits job events.
- A service that needs "today" takes it as an argument (`today: date`). That single habit makes every date rule testable.

### Repositories (`repositories/`)
- SQL only. One module per aggregate.
- Return domain dicts or model objects — never HTTP shapes.
- **Upsert semantics, not skip semantics** for tenders (BP §6.2).
- **Carry-forward merges must never write NULL over a good value.** In SQL that means `COALESCE(new, old)` on every optional field of a re-read. Write the test (see §9).
- No `SELECT *`. No cross-aggregate joins without a comment saying why.

---

## 5. Transactions

One pattern, used everywhere:

- The request handler opens a session.
- The **service** decides the unit of work and commits once at the end.
- Repositories receive the session; they never open their own.
- A job performs the same pattern per unit of work; if a job does several units, commit per unit.

Why it matters concretely: "attach document → create pages → extract requirements → generate checklist" is five writes. Without one transaction, a crash leaves requirements citing pages that do not exist, which breaks the product's first promise (BP Part 2, rule 1).

---

## 6. Naming

| Thing | Convention | Example |
|---|---|---|
| Table | snake_case, plural | `tender_documents` |
| Column | snake_case, singular | `closing_at` |
| Timestamp | suffix `_at` | `saved_at`, `captured_at` |
| Date (no time) | suffix `_date` | `published_date` |
| Money | suffix `_amount` + currency column | `value_amount`, `currency` |
| Boolean | `is_` / `has_` | `is_current`, `has_text_layer` |
| Enum | lowercase string, explicit values | `verified`, `accepted_ocr` |
| FK | `<entity>_id` | `requirement_id` |
| Route | plural nouns, no verbs where possible | `GET /api/tenders/{id}/documents` |
| Service | `*_service.py` (orchestration), `*_rules.py` (pure) | `assessment_rules.py` |
| Repository | `<aggregate>_repository.py` | `tenders_repository.py` |
| Job kind | verb_noun | `fetch_documents`, `scrape_portal` |

---

## 7. Recipes

### Add a table
1. Model in `models/<aggregate>.py`.
2. Alembic migration — **hand-written**, with the constraints from BP §6.3: PK, unique, FK, `ON DELETE` behaviour, and the indexes you will actually query.
3. Repository methods.
4. Tests: unique-violation test, cascade/restrict test, and one happy-path read.

Never add a table without its unique constraint. Uniqueness is the dedupe mechanism, not an application `if`.

### Add a repository method
- Signature takes `session` first, `company_id` where judgement data is involved.
- Returns domain objects; raises typed errors, not `Exception`.
- If it writes, it is either an insert, an update, or an **upsert with carry-forward**.

### Add an endpoint
1. Pydantic schema in `schemas/`.
2. Route: resolve company, call service, return.
3. Service: transaction, rules, repository calls.
4. Tests: auth required, wrong-company 404, happy path, and the state the endpoint is meant to expose.

### Add a job kind
1. Add the enum value.
2. Handler: pure step function. **It must be safe to run twice** — check state before acting.
3. Set an `idempotency_key` when enqueueing (e.g. `fetch_documents:tender:<id>:<attempt>`), so a double-click cannot double-fetch.
4. Register it in the job registry — the one place where a module declares its kinds.
5. Test: kill-and-resume, and double-enqueue.

### Add a pure rule
1. New `*_rules.py`, plain arguments, plain return.
2. Table-driven tests: one row per case, including the negative ones.
3. If it needs "now", take it as a parameter.

---

## 8. Jobs and lanes

A job is a row, not a thread.

| State | Meaning | Who moves it |
|---|---|---|
| `queued` | waiting | worker claims it |
| `running` | a worker holds a lease | worker completes/fails/parks |
| `needs_you` | waiting on a human action in the visible browser | the driver sees the action, a manual `POST /challenge` signal, or expiry |
| `done` | finished | terminal |
| `partial` | finished with a known gap | terminal, but visible |
| `failed` | with a readable reason | retry policy |

Rules:
- **Heartbeat while running.** A stale lease returns the job to `queued`; that is the entire crash-recovery story.
- **Check a cancel flag at step boundaries**, never by killing a thread.
- **Park the worker, not the browser.** `needs_you` returns the worker thread to the pool, but keeps the browser session under a bounded `lane_leases` reservation (`reserved_for_challenge`). Other browser jobs queue; non-browser jobs continue. Releasing the lane would lose the pending file request.
- **Never receive or store the captcha answer.** The human types in the portal page. `POST /api/jobs/{id}/challenge` signals only; the driver watches the session for the download/navigation event. Expiry drops the session and records `partial` with "file request expired".
- **A user-initiated job outranks a scheduled one.**
- **Never enqueue from inside a transaction that might roll back** - enqueue after commit, or in the same transaction as the state it depends on. Decide which, and comment it.

Lanes: `lane_leases(name PK, owner_kind, owner_id, acquired_at, lease_expires_at, reserved_for_challenge, expires_at)`. One row per shared resource (`browser`), acquired with an expiry. Never an in-process mutex, and never `app_state` (which has no expiry). The app restarts.

---

## 9. Testing

Four layers, and each has a job:

| Layer | What it proves | DB |
|---|---|---|
| `test_rules_*` | the decision logic | none — fast, exhaustive |
| `test_repo_*` | the SQL and the constraints | ephemeral Postgres, truncate between tests |
| `test_api_*` | the HTTP contract and company scoping | ephemeral Postgres |
| `test_jobs_*` | state transitions and resume | ephemeral Postgres |

Rules:
- **Never mock SQL.** Mocking the database you are migrating onto defeats the migration.
- **Never assert on internals** (private methods, call counts) when the state transition can be asserted instead.
- A bug found in production gets a regression test **named after the behaviour**, not the ticket.

**Must-have regression tests** — each one maps to a trap in BP Part 18:

1. A second tender read does not erase a field the first read had (carry-forward).
2. Re-running discovery on the same day creates no duplicate tender.
3. Two snapshots in the same millisecond never invert order.
4. A date-only deadline survives as the same IST calendar day.
5. A fabricated excerpt is rejected; an OCR-only match can never be `verified`.
6. `Unknown` never becomes `Pass` because the score is high.
7. A non-PDF is reported, never attached.
8. A job killed mid-run resumes to the same state.
9. Catch-up fires when the last completed run is older than the interval, and only once.
10. A rejected requirement is still visible to another company.

---

## 10. Errors, states and messages

- **Typed errors** in `core/errors.py`; routes map them to status codes. One place decides.
- **User-facing messages are sentences about the data**, not about the code. "4 of 5 documents — cover 3 (BOQ) missing" beats "partial".
- **Never let an exception become a silent empty result.** If a fetch returned nothing, say why: read nothing, unlinked, filtered, or failed.
- Distinct states for distinct truths: `unverified` ≠ `rejected` ≠ `needs_review` ≠ `accepted_ocr`. Do not collapse them for tidiness.
- Log lines carry `job_id`, `tender_id`, `company_id` where they exist. No secrets, no full request bodies.

---

## 11. Do not

- `ObjectId`, `motor`, `pymongo`, `mongomock` — removed permanently (BP §16.4).
- Store a portal URL as if it were an identifier — store a **locator**.
- Write `NULL` over an existing field during a merge.
- Let the AI's output reach a stored field, a score, or the UI without passing deterministic validation.
- Set `verified` from a human action.
- Put `company_id` on a public-corpus table.
- Treat "no evidence" as a pass, ever.
- Auto-fetch documents on save, or on anything other than "marked as bidding".
- Start a second browser session while one holds the lane.
- Ask the user for portal credentials.
- Add a `CREATE TABLE` without a unique constraint and a delete policy.
- Put business rules in routes, SQL in services, or FastAPI imports in services.
- `float` for money; naive `datetime` for deadlines.
- Silently swallow an exception into an empty list.

---

## 12. Commands

```powershell
# backend
cd backend; .\.venv\Scripts\python.exe -m pytest
cd backend; .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
cd backend; .\.venv\Scripts\python.exe -m alembic upgrade head

# frontend
cd frontend; npm run dev
cd frontend; npm test
cd frontend; npm run build

# demo / ops
.\backend\.venv\Scripts\python.exe scripts\seed-demo.py --email <owner-email>
.\backend\.venv\Scripts\python.exe scripts\send-reminders.py
```

Postgres is the only required service. No MongoDB, no Docker, no vector store (until BP §5.11 resumes).

Run the migration before the server: startup compares the database's
`alembic_version` stamp with the migration head and refuses to serve a schema
that is behind it (`SCHEMA_CHECK=false` only to boot a database you are about
to migrate, never as a deployment mode). A stale schema otherwise fails one
request at a time with an undefined table that looks like a code bug.

---

## 13. Definition of done, per change

Before you call it finished:

- [ ] Layers respected; no SQL in routes, no rules in repositories.
- [ ] Multi-step writes in one transaction.
- [ ] Every new table has a unique constraint and an explicit `ON DELETE`.
- [ ] A migration exists and applies to an **empty** database.
- [ ] New rules are pure and table-tested, including the negative cases.
- [ ] The regression tests in §9 that this change touches still pass.
- [ ] Failure states are stored and visible, not swallowed.
- [ ] No `company_id` leaked onto public-corpus tables; no cross-company read possible.
- [ ] UTC everywhere; date-only values anchored at noon UTC.
- [ ] If a convention changed, **this file changed in the same commit**.
- [ ] If a decision changed, **the blueprint's decision log changed too**.

---

## 14. Where to look

| I need… | Go to |
|---|---|
| the rule for X | BP Part 2 (product) or §1 here (code) |
| a table's keys | BP §6.3 |
| **a table's columns, types or enums** | `SCHEMA-SPEC.md` |
| **an endpoint or status code** | `API-SURFACE.md` |
| why corrigenda are separate rows | BP §6.7 |
| why a session dies and what a locator is | BP §5.4 |
| job states and the failure table | BP §5.5, Part 9 |
| the citation gate | BP §5.8 |
| score vs gate | BP §5.9 |
| traps that already cost time | BP Part 18 |
| what is built vs deferred | BP §16.2, Part 19 |
| why a choice was made | BP Part 15 (decision log), Appendix B |
