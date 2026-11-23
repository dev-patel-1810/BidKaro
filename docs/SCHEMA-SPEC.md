# Schema specification (Postgres)

**Purpose:** column-level truth for the rebuild. This file is the input to the Stage A Alembic migration — the migration should be a transcription of it, not a design exercise.

**Rules that apply to every table** (from `CODING-GUIDE.md` §1 and blueprint §6):

- Primary keys are `uuid` with `gen_random_uuid()`.
- Timestamps are `timestamptz`, always UTC. Date-only business deadlines are anchored at **12:00 UTC** so the IST calendar day survives.
- Money is `numeric(18,2)` plus a `currency` column. Never `float`.
- Soft delete uses `deleted_at timestamptz null`; it never replaces a `RESTRICT` foreign key.
- Every table has `created_at timestamptz not null default now()`.
- Enums are stored as `text` with a `CHECK` constraint (listed in §1), not Postgres `ENUM` types — values change more often than a type migration should.
- No `SELECT *`. No `company_id` on a public-corpus table.

---

## 1. Enum catalogue

Every state value in the system, defined once. An implementer who needs a new one adds it here **and** to the `CHECK` constraint in the same commit.

| Enum | Values | Used by |
|---|---|---|
| `requirement_state` | `verified`, `needs_review`, `accepted_ocr`, `unverified`, `rejected` | `requirements.state` |
| `requirement_category` | `eligibility`, `submission`, `compliance`, `form`, `performance` | `requirements.category` |
| `requirement_phase` | `pre_award`, `post_award` | `requirements.phase`, `checklist_tasks.phase` |
| `mandatory_state` | `mandatory`, `not_mandatory`, `unknown` | `requirements.mandatory_state` |
| `job_state` | `queued`, `running`, `needs_you`, `done`, `partial`, `failed`, `cancelled` | `jobs.state` |
| `job_kind` | `scrape_portal`, `sweep_portal`, `classify_corpus`, `fetch_documents`, `refresh_tender`, `extract_requirements`, `extract_company_document`, `judge_tender_similarity`, `snapshot_tender`, `revalidate_citations`, `send_reminders` | `jobs.kind` |
| `lane` | `browser` | `jobs.lane`, `lane_leases.name` |
| `gate_result` | `pass`, `fail`, `unknown` | `fit_assessments.gate` |
| `assessment_verdict` | `pass`, `fail`, `unknown` | `assessment_criteria.verdict` |
| `evidence_status` | `covered`, `missing`, `expired`, `contradicted` | `evidence_matches.status` |
| `checklist_status` | `open`, `in_progress`, `done`, `blocked`, `not_applicable` | `checklist_tasks.status` |
| `reminder_state` | `open`, `sent`, `dismissed` | `reminders.state` |
| `change_type` | `field_changed`, `requirement_added`, `requirement_removed`, `requirement_changed`, `document_added`, `lineage_linked` | `changes.change_type` |
| `change_review_state` | `pending`, `acknowledged`, `dismissed` | `changes.review_state` |
| `source_type` | `official_portal`, `authority`, `aggregator`, `manual_upload` | `tenders.source_type` |
| `extracted_from` | `listing_table`, `search_result`, `notice_page`, `manual_upload` | `tenders.extracted_from` |
| `document_type` | `about_company`, `registration`, `iso`, `turnover`, `completion_certificate`, `pan`, `gst`, `other` | `company_documents.document_type` |
| `document_extraction_state` | `pending`, `running`, `done`, `failed`, `no_text_layer` | `tender_documents.extraction_state` |
| `otp_purpose` | `signup`, `login` | `otp_codes.purpose` |
| `challenge_kind` | `captcha`, `login_wall`, `otp` | `challenges.kind` |
| `challenge_state` | `open`, `answered`, `expired`, `abandoned` | `challenges.state` |
| `relation_state` | `confirmed`, `proposed`, `rejected` | `tenders.supersedes_state` |
| `candidate_kind` | `source`, `tender_candidate` | `discovery_candidates.kind` |
| `candidate_outcome` | `stored`, `rejected`, `unreadable`, `unlinked` | `discovery_candidates.outcome` |
| `cycle_state` | `active`, `terminated` | `tenders.cycle_state` |

**Note on `requirement_state`.** `verified` is set **only** by the citation gate. `accepted_ocr` is a human accepting an OCR reading - it is deliberately a different value and must never be collapsed into `verified` (blueprint §6.5). It may satisfy a mandatory criterion's `Pass` only with human confirmation and recorded OCR provenance (blueprint §5.9).

---

## 1a. Amendments 2026-10-26 (binding)

Where a table below disagrees with this section, **this section wins**. The
initial migration `0001_initial` and `0002_otp_payload` are the transcription.

1. **Corpus is global.** No `company_id` on `tenders`, `tender_documents`,
   `requirements`, `changes`, `discovery_candidates`. Company judgement lives
   only in `company_tenders`, `company_requirement_flags`, `evidence_matches`,
   `assessment_criteria`, `fit_assessments`, `checklist_tasks`, `reminders`.
   Crawl targeting uses the union of `companies` where `is_active`; storage is
   profile-free; listing relevance is computed at read time.
2. **Dedupe key has no dates.** `sha256(portal_id | normalised_reference |
   normalised_work_title | normalised_organisation)`. Dates belong to snapshots.
   `portal_id` and `portal_tender_id` are nullable; portal identity is
   `(portal_id, portal_tender_id)` where the id is present.
3. **Two upsert keys, one service function.** Portal id present: upsert on
   `(portal_id, portal_tender_id)`. No portal id: upsert on `dedupe_key`. If both
   match different rows, the portal row is canonical; the aggregator row gets
   `merged_into_tender_id` and `merged_at`, is hidden from listings, and keeps
   its children (children are not physically moved in v1).
4. **Manual tenders are legitimate.** `source_type='manual_upload'`, nullable
   portal, `dedupe_key = sha256('manual' | normalised_work_title |
   first_document_sha256)`.
5. **Sources and rejected candidates are rows, not counts.**
   `discovery_candidates(id, run_id, url, kind, outcome, reason, listing_url,
   first_seen_at, last_seen_at)`; `kind ∈ source|tender_candidate`,
   `outcome ∈ stored|rejected|unreadable|unlinked`. `GET /api/discovery/sources`
   reads `kind='source'`.
6. **Cross-portal duplicates are accepted.** Add non-unique `cross_portal_key`
   (normalised reference + closing date + title hash) for "also on the state
   portal"; never auto-merge.
7. **Requirement fields.** Add `title`, `obligation` (model interpretation,
   explicitly non-citable), `deadline_text`, `deadline_at`, `consequence_text`,
   `acceptance_criteria`, `issuer`, `signatory`, `evidence_check`, `form_ref`,
   `model`, `prompt_version`. Replace `is_mandatory boolean` with
   `mandatory_state ∈ mandatory|not_mandatory|unknown` default `unknown`.
8. **Excerpt uniqueness is per page.** `UNIQUE (document_id, page_index,
   excerpt_hash) WHERE is_active`. Add `is_navigation boolean`; navigation hits
   are stored but excluded from assessment and hidden by default.
9. **Requirements are never hard-deleted.** Add `is_active boolean` default
   `true`; `replace=true` sets AI-origin rows inactive and re-extracts.
   Dependants keep pointing at rows that still exist.
10. **Checklist provenance is copied at generation.** `checklist_tasks` copies
    `document_id`, `page_index`, `clause_ref`, `excerpt`, `source_url`. Natural
    key `UNIQUE (company_id, tender_id, requirement_id, phase)`; multiple tasks
    per requirement wait for a future `task_key`.
11. **Per-criterion gate.** Add `assessment_criteria(id, company_id, tender_id,
    requirement_id, criterion_text, verdict pass|fail|unknown, reason,
    evidence_match_id, is_active)`. Aggregate: any `Fail` -> `Fail`; else any
    `Unknown` -> `Unknown`; else `Pass`.
12. **Score formula.** `25 × matched profile terms / total profile terms` +
    `20 × (verified + accepted_ocr) / active requirements` + `40 × mandatory
    criteria with confirmed non-expired evidence / mandatory criteria` + `15 ×
    documents present / covers expected (0 if no covers known)`, rounded 2dp.
13. **Evidence is pinned to a version.** `evidence_matches
    .company_document_version_id` (not the parent), `ON DELETE RESTRICT` plus
    soft delete. Expiry or soft delete recomputes the match to `expired`/`missing`
    and clears `confirmed_by`.
14. **Evidence mapping.** `covered` -> Pass-eligible; `expired` and
    `contradicted` -> Fail; `missing`/none -> Unknown. `accepted_ocr` may satisfy
    `Pass` and is displayed "Pass - OCR citation, human-confirmed".
15. **Change and reminder states restored.** `changes.review_state
    (pending|acknowledged|dismissed)`, `reviewed_at`, `reviewed_by`,
    `source_url`. `reminders.acknowledged_at`, `lead_days`, `attempts`,
    `last_error`. `companies.reminder_opt_in`, `companies.reminder_lead_days`.
16. **Document lifecycle.** Add `extraction_state
    (pending|running|done|failed|no_text_layer)`, `extraction_error`,
    `document_key`, `supersedes_document_id`; `is_current` is scoped per
    `(tender_id, document_key)`. Assessment uses current versions only.
17. **Browser lane table.** `lane_leases(name PK, owner_kind, owner_id,
    acquired_at, lease_expires_at, reserved_for_challenge, expires_at)`.
    `app_state` cannot serve because it has no expiry.
18. **Auth is enumeration-resistant.** Register and request-code are 202 for
    known, pending and unknown addresses. The user/company are created on
    successful signup verification; pending signup is an `otp_codes` row with
    `payload jsonb` holding the hashed password and company name. The JWT
    carries `sub` and `company_id`, but the server reloads and authorises from
    the database. `PATCH /api/auth/security` accepts the current password or a
    fresh email code; a code-only user must set a password before disabling
    code sign-in.
19. **Jobs are canonical.** `/api/discovery/runs/{id}` is a thin alias for
    `/api/jobs/{id}` filtered to `kind='scrape_portal'`; reporting rows are
    addressed at `/api/jobs/{id}/runs`. One id space in the UI.
20. **Global annotation is advisory.** `PATCH /api/requirements/{id}` may set
    only `needs_review`, `rejected` or `accepted_ocr`, never `verified`, and is
    audited with the actor. `rejected` stays visible to all; a company hides it
    with `company_requirement_flags`. There is no DELETE for tender documents.
21. **Captcha is visible-browser only.** `challenges` has no answer column and
    `POST /challenge` carries no answer. A `needs_you` job releases its worker
    thread but holds the browser session under a bounded lease; the lane is
    `reserved_for_challenge`. Expiry drops the session and records `partial`.
22. **Scraping is a module.** The base does not register `/api/discovery/*` or
    `POST /api/tenders/{id}/documents/fetch`; `/api/health` reports
    `features.discovery=false` and the frontend hides those affordances.
23. **No roles.** Any authenticated user may annotate within the limits above;
    an admin role is not built to cover a capability that was deleted.
24. **Company documents** are company-scoped and soft-deleted. They carry
    `signatory`, `financial_year`, `project_value_amount`,
    `project_value_currency`, `verification_status` and `metadata jsonb`.
25. **`company_requirement_flags`** is the per-company hide/note table
    (`UNIQUE (company_id, requirement_id)`), separate from the global
    requirement state.
26. **Port, do not invent.** Every deterministic rule is ported from the old
    implementation with a fixture pinning its expected output before the old
    tree is deleted.

## 2. Identity and tenancy

### `companies`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `name` | text | no | not unique — real company names collide |
| `region` | text | yes | orders results, never filters them |
| `sectors` | text[] | no | default `'{}'`, GIN indexed |
| `services` | text[] | no | default `'{}'`, GIN indexed |
| `keywords` | text[] | no | default `'{}'`, GIN indexed |
| `profile` | jsonb | no | default `'{}'`, anything not filtered on |
| `is_active` | boolean | no | default `true`; crawler targets the union of active profiles |
| `reminder_opt_in` | boolean | no | default `false` |
| `reminder_lead_days` | integer[] | no | default `'{7,3,1}'` |
| `created_at`, `updated_at` | timestamptz | no | |

### `users`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK → companies, `ON DELETE RESTRICT` |
| `email` | citext | no | **UNIQUE** |
| `password_hash` | text | yes | null for code-only accounts |
| `full_name` | text | yes | |
| `email_verified` | boolean | no | default `false` |
| `email_otp_enabled` | boolean | no | default `false` |
| `is_active` | boolean | no | default `true` |
| `last_login_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

### `company_documents` (evidence library)

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `document_type` | text | no | `document_type` enum |
| `title` | text | no | e.g. "ISO 9001:2015 certificate" |
| `issuer` | text | yes | |
| `signatory` | text | yes | |
| `issued_on` | date | yes | |
| `expires_on` | date | yes | **the column that makes `Fail` possible** |
| `financial_year` | text | yes | e.g. `2024-25` |
| `project_value_amount` | numeric(18,2) | yes | for completion certificates |
| `project_value_currency` | char(3) | no | default `INR` |
| `notes` | text | yes | |
| `verification_status` | text | no | default `self_declared`; `self_declared|verified|rejected` |
| `metadata` | jsonb | no | default `'{}'` |
| `extraction_state` | text | no | default `pending`; `pending|done|no_text_layer|skipped|unsupported|failed` |
| `extraction_error` | text | yes | readable reason; never a substitute for a term |
| `extracted_at` | timestamptz | yes | last completed read; kept on a failed re-read |
| `term_count` | integer | no | default `0`; the current term rows, kept on a failed re-read |
| `understanding_state` | text | no | default `pending`; `pending|done|no_ai|no_text_layer|failed` |
| `understanding_error` | text | yes | readable reason; never a substitute for a citation |
| `understanding_at` | timestamptz | yes | last completed understanding read |
| `capability_count` | integer | no | default `0`; capability rows for this document |
| `experience_count` | integer | no | default `0`; experience rows for this document |
| `deleted_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: `(company_id, document_type)`, `(company_id, expires_on)`.

Only `about_company` is read for matching. Every other type is evidence and
stays `pending`; an upload of an `about_company` PDF enqueues
`extract_company_document`. Reading a company document never touches
`verification_status`, `expires_on` or any eligibility input.

### `company_document_versions`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_document_id` | uuid | no | FK, `CASCADE` |
| `sha256` | char(64) | no | **UNIQUE (company_document_id, sha256)** |
| `storage_path` | text | no | content-addressed |
| `mime` | text | no | |
| `bytes` | bigint | no | |
| `uploaded_at` | timestamptz | no | |

### `company_document_pages` (derived)

Page text kept from the same PDF/OCR reader the tender documents use. It is the
evidence behind an extracted work term, exactly as a tender page is the evidence
behind a cited requirement.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_document_version_id` | uuid | no | FK, `CASCADE` |
| `page_index` | integer | no | 0-based viewer page |
| `printed_label` | text | yes | the PDF's own page label |
| `text` | text | no | default `''` |
| `char_count` | integer | no | default `0` |
| `is_ocr` | boolean | no | default `false`; true when the text came from OCR |
| `created_at` | timestamptz | no | |

Indexes: **UNIQUE `(company_document_version_id, page_index)`**.

### `company_document_terms` (derived)

One work phrase or word read from an `about_company` profile, with its page and
verbatim line. Terms are a matching signal, never eligibility.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `company_document_id` | uuid | no | FK, `CASCADE` |
| `term` | text | no | display text (phrase or word) |
| `normalized` | text | no | casefolded, whitespace-collapsed |
| `page_index` | integer | no | |
| `printed_label` | text | yes | |
| `excerpt` | text | no | the verbatim line, bounded |
| `source` | text | no | default `text_rule` |
| `rules_version` | text | no | the harvest rules that produced it |
| `created_at` | timestamptz | no | |

Indexes: **UNIQUE `(company_document_id, normalized)`**, `(company_id)`,
`(company_id, normalized)`.

Terms are read live and excluded when the document is soft-deleted or its type
is no longer `about_company`. Removing one row changes suggestions only.

### `company_capabilities` (derived, human-confirmed)

A kind of work the company says it does, read from an `about_company` profile
and grounded in the page and verbatim line it came from. Only `confirmed` rows
are shown to the similarity judge; a `proposed` row is visible but unused.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `company_document_id` | uuid | no | FK, `CASCADE` |
| `name` | text | no | plain-language capability |
| `normalized` | text | no | matching/dedupe form |
| `description` | text | yes | bounded |
| `aliases` | text[] | no | default `{}`; alternative names |
| `page_index` | integer | no | 1-based page the quote is on |
| `printed_label` | text | yes | the PDF's own page label |
| `excerpt` | text | no | the verbatim line; quote-checked before insert |
| `quote_verified` | boolean | no | default `true`; false when OCR made the quote uncertain |
| `is_ocr` | boolean | no | default `false` |
| `state` | text | no | default `proposed`; `proposed|confirmed|rejected` |
| `origin` | text | no | default `ai`; `ai|human` (an edit sets `human`) |
| `model`, `prompt_version` | text | yes | provenance of the extraction |
| `confirmed_by` | uuid | yes | FK users, `SET NULL` |
| `confirmed_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: **UNIQUE `(company_document_id, normalized)`**,
`(company_id, state)`.

### `company_experiences` (derived, human-confirmed)

A named project or product the company delivered, with the same grounding and
confirmation rules as a capability. A named experience is stronger evidence
than a general capability when mapping a tender requirement.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `company_document_id` | uuid | no | FK, `CASCADE` |
| `title` | text | no | project/product name |
| `normalized` | text | no | matching/dedupe form |
| `client` | text | yes | client or authority named by the profile |
| `description` | text | yes | bounded |
| `work_family` | text | yes | the family of work the profile names |
| `value_amount` | numeric(18,2) | yes | only when stated |
| `currency` | text | no | default `INR` |
| `completed_on` | date | yes | only when stated |
| `page_index` | integer | no | 1-based page |
| `printed_label` | text | yes | |
| `excerpt` | text | no | verbatim line |
| `quote_verified` | boolean | no | default `true` |
| `is_ocr` | boolean | no | default `false` |
| `state` | text | no | default `proposed`; `proposed|confirmed|rejected` |
| `origin` | text | no | default `ai`; `ai|human` |
| `model`, `prompt_version` | text | yes | |
| `confirmed_by` | uuid | yes | FK users, `SET NULL` |
| `confirmed_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: **UNIQUE `(company_document_id, normalized)`**,
`(company_id, state)`.

Both tables are derived from the document: deleting or reclassifying the profile
cascades the rows away, and only confirmed rows are read by matching.

### `tender_similarity_judgments` (cached, per company)

One closed-label answer per company and tender: `yes`, `no` or `unsure`. It is a
second signal on top of the rules verdict and never an eligibility answer.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `tender_id` | uuid | no | FK, `CASCADE` |
| `input_hash` | text | no | covers tender content hash + confirmed capability signature + rules version |
| `answer` | text | no | `yes|no|unsure` |
| `anchor_text` | text | yes | the words the model quoted from the notice |
| `anchor_verified` | boolean | no | default `false`; true only when found in the notice |
| `needs_confirmation` | boolean | no | default `false`; an unanchored `yes` is stored `unsure` + true |
| `reason` | text | yes | deterministic reason written by our code |
| `matched_capability_ids` | uuid[] | no | default `{}` |
| `model`, `prompt_version`, `rules_version` | text | yes | audit trail |
| `judged_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: **UNIQUE `(company_id, tender_id)`**. A cached row is reused only
while `input_hash` matches, so a capability edit or a re-published notice causes
a fresh judgment.

### `requirement_experience_matches` (per company)

What the company's own confirmed work shows for one requirement:
`covered`, `partial`, `new` or `unknown`. The company-side citation is copied
from the stored capability/experience row, never from model output.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `tender_id` | uuid | no | FK, `CASCADE` |
| `requirement_id` | uuid | no | FK, `CASCADE` |
| `coverage` | text | no | `covered|partial|new|unknown` |
| `experience_ids`, `capability_ids` | uuid[] | no | default `{}`; used only for covered/partial |
| `company_document_id` | uuid | yes | FK company_documents, `SET NULL` |
| `company_page_index` | integer | yes | 1-based page |
| `company_excerpt` | text | yes | verbatim line from the profile |
| `requirement_excerpt` | text | yes | the tender-side excerpt as it was compared |
| `reason` | text | yes | deterministic reason |
| `model`, `prompt_version` | text | yes | |
| `input_hash` | text | yes | unchanged requirements reuse their row |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: **UNIQUE `(company_id, requirement_id)`**,
`(company_id, tender_id)`. A requirement the reader skipped is stored `unknown`,
never `new`, because silence is not evidence that the company has never done the
work.

---

## 3. Global corpus

### `portals`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `code` | text | no | **UNIQUE**, e.g. `cppp` |
| `name` | text | no | |
| `base_url` | text | yes | |

### `tenders`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `portal_id` | uuid | yes | FK → portals; nullable for a manual tender |
| `portal_tender_id` | text | yes | e.g. `2026_NHAI_295179_1` |
| `dedupe_key` | char(64) | no | **UNIQUE**; sha256 of portal + normalised reference + normalised title + normalised organisation. **No dates.** |
| `reference` | text | yes | as printed |
| `reference_normalised` | text | yes | indexed, **never unique** |
| `authority` | text | yes | |
| `organisation_chain` | text | yes | |
| `work_title` | text | yes | |
| `work_type` | text | yes | matched against the profile |
| `location` | text | yes | |
| `region` | text | yes | |
| `closing_at` | timestamptz | yes | noon UTC when the source gave a date only |
| `published_at` | timestamptz | yes | never used as the closing date |
| `value_amount` | numeric(18,2) | yes | |
| `currency` | char(3) | no | default `INR` |
| `source_type` | text | no | `source_type` enum, including `manual_upload` |
| `extracted_from` | text | no | `extracted_from` enum |
| `source_url` | text | yes | provenance only - **never an identifier** |
| `retrieved_at` | timestamptz | yes | |
| `content_hash` | char(64) | yes | of the normalised fields + requirement fingerprints |
| `cycle_state` | text | no | `cycle_state` enum; default `active`; `terminated` only when the portal's own page states the cycle is over |
| `termination_excerpt` | text | yes | the portal's verbatim sentence - the evidence for the state |
| `termination_observed_at` | timestamptz | yes | when that sentence was read |
| `unverified` | text[] | no | default `'{}'` - agent/model claims the page did not state |
| `supersedes_tender_id` | uuid | yes | FK → tenders (self), `SET NULL` |
| `supersedes_state` | text | yes | `relation_state` enum; only `confirmed` hides the original |
| `merged_into_tender_id` | uuid | yes | FK → tenders (self), `SET NULL`; aggregator row merged into a portal row |
| `merged_at` | timestamptz | yes | |
| `cross_portal_key` | char(64) | yes | non-unique; "also on the state portal" |
| `raw` | jsonb | yes | the verbatim row/page evidence |
| `deleted_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

**Constraints**

- `UNIQUE (portal_id, portal_tender_id) WHERE portal_tender_id IS NOT NULL` - a real portal id always wins.
- `UNIQUE (dedupe_key)` - the fallback identity.
- `CHECK (supersedes_tender_id IS NULL OR supersedes_tender_id <> id)`.
- `CHECK (merged_into_tender_id IS NULL OR merged_into_tender_id <> id)`.
- `CHECK (cycle_state IN ('active','terminated'))`.

**Indexes:** `(closing_at)`, `(portal_id, published_at DESC)`, `(reference_normalised)`, `(content_hash)`, `(supersedes_tender_id)`, `(merged_into_tender_id)`, `(cross_portal_key)`.

**Merge rule.** Upserts fill gaps and never null an existing value: every optional column is written as `COALESCE(excluded.col, tenders.col)`. This is a must-have regression test (blueprint Part 18 #26–27, guide §9).

### `tender_locators`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `tender_id` | uuid | no | **UNIQUE**, FK `CASCADE` |
| `portal_id` | uuid | no | FK |
| `portal_tender_id` | text | yes | the thing you navigate by |
| `reference` | text | yes | |
| `organisation_chain` | text | yes | |
| `work_title` | text | yes | |
| `published_on` | date | yes | helps find the row in a list |
| `last_verified_at` | timestamptz | yes | last time a session actually reached it |

### `tender_covers`

The portal's own document list, stored **per version** so it can be diffed (blueprint §5.7).

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `tender_id` | uuid | no | FK `CASCADE` |
| `version` | text | no | `v1`, `v2` |
| `cover_no` | integer | no | |
| `doc_type` | text | yes | "BOQ", "EMD", "RFP" |
| `description` | text | yes | matched across versions; `cover_no` alone is not stable |

`UNIQUE (tender_id, version, cover_no)`.

### `tender_documents`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `tender_id` | uuid | no | FK `CASCADE` |
| `filename` | text | no | |
| `sha256` | char(64) | no | **UNIQUE (tender_id, sha256)** |
| `version` | text | no | `v1`, `v2` |
| `document_key` | text | no | stable logical identity, e.g. normalised filename + tender |
| `supersedes_document_id` | uuid | yes | FK → tender_documents (self), `SET NULL` |
| `is_current` | boolean | no | default `true` |
| `storage_path` | text | no | content-addressed |
| `mime` | text | no | |
| `bytes` | bigint | no | |
| `page_count` | integer | yes | |
| `has_text_layer` | boolean | no | default `true` |
| `ocr_page_count` | integer | no | default `0` |
| `extraction_state` | text | no | default `pending`; `document_extraction_state` |
| `extraction_error` | text | yes | readable |
| `source_url` | text | yes | provenance |
| `fetched_at` | timestamptz | yes | |
| `deleted_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Index: `(tender_id, version, is_current)`.
Partial unique: `UNIQUE (tender_id, document_key) WHERE is_current AND deleted_at IS NULL`.

### `document_fetch_state`

Global, one row per tender — a second company must not re-pay a captcha (blueprint §5.7).

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `tender_id` | uuid | no | **UNIQUE**, FK `CASCADE` |
| `last_requested_at` | timestamptz | yes | |
| `last_attempt_at` | timestamptz | yes | |
| `attempts` | integer | no | default `0` |
| `expected_covers` | integer | yes | from the cover table |
| `documents_present` | integer | no | default `0` |
| `missing_covers` | integer[] | no | default `'{}'` |
| `last_error` | text | yes | readable sentence |

### `document_pages`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `document_id` | uuid | no | FK `CASCADE` |
| `page_index` | integer | no | viewer page, 1-based |
| `printed_label` | text | yes | the page's own number when it differs |
| `text` | text | no | default `''` |
| `char_count` | integer | no | default `0` |
| `is_ocr` | boolean | no | default `false` — OCR text must never pass as a text layer |

`UNIQUE (document_id, page_index)`.

### `requirements`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `document_id` | uuid | no | FK `CASCADE` |
| `page_index` | integer | no | |
| `printed_label` | text | yes | |
| `clause_ref` | text | yes | "Clause 4.2", "Annexure C" |
| `form_ref` | text | yes | e.g. "Form 3" |
| `category` | text | no | `requirement_category` |
| `phase` | text | no | default `pre_award` |
| `title` | text | yes | short label for lists |
| `obligation` | text | yes | model's interpretation, **never a citation** |
| `deadline_text` | text | yes | as printed |
| `deadline_at` | timestamptz | yes | anchored at noon UTC for a date-only value |
| `consequence_text` | text | yes | only when the source states it |
| `acceptance_criteria` | text | yes | extracted, or null with "not stated in source" |
| `issuer` | text | yes | |
| `signatory` | text | yes | |
| `evidence_check` | text | yes | |
| `excerpt` | text | no | verbatim |
| `excerpt_normalised` | text | no | lowercased, whitespace-collapsed |
| `excerpt_hash` | char(64) | no | **partial UNIQUE (document_id, page_index, excerpt_hash) WHERE is_active** |
| `is_navigation` | boolean | no | default `false`; contents/index hits are stored but excluded from assessment |
| `state` | text | no | `requirement_state` |
| `state_reason` | text | yes | why unverified/needs_review |
| `mandatory_state` | text | no | default `unknown`; `mandatory_state` enum |
| `origin` | text | no | default `ai` - `ai` or `human`; `replace=true` deactivates AI rows, never deletes |
| `model` | text | yes | extraction provenance |
| `prompt_version` | text | yes | extraction provenance |
| `is_active` | boolean | no | default `true`; dependants keep their FK |
| `reviewed_by` | uuid | yes | FK → users |
| `reviewed_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: `(document_id, state)`, `(state)` for the re-validation sweep,
`(document_id, page_index)`.

**Human edits.** A human may change `category`, `mandatory_state` and `title`
(audited) and may add an `origin='human'` requirement. A human may never set
`verified`; that is the citation gate's value.

### `tender_snapshots`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `tender_id` | uuid | no | FK `CASCADE` |
| `captured_at` | timestamptz | no | strictly increasing per tender; stepped by ≥1 ms |
| `content_hash` | char(64) | no | fields + requirement fingerprints |
| `fields` | jsonb | no | normalised tender fields |
| `requirement_fingerprints` | jsonb | no | hash + state per requirement |

`UNIQUE (tender_id, captured_at)`. Sort by `(captured_at DESC, id DESC)` — the id tie-breaker is not optional.

### `changes`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `tender_id` | uuid | no | FK `CASCADE` |
| `from_snapshot_id` | uuid | yes | FK `CASCADE` |
| `to_snapshot_id` | uuid | no | FK `CASCADE` |
| `change_type` | text | no | `change_type` enum |
| `field_key` | text | no | e.g. `closing_at`, or `req:<hash>` |
| `old_value` | text | yes | |
| `new_value` | text | yes | |
| `detected_at` | timestamptz | no | |
| `review_state` | text | no | default `pending`; `change_review_state` |
| `reviewed_at` | timestamptz | yes | |
| `reviewed_by` | uuid | yes | FK → users |
| `source_url` | text | yes | where the changed value was read |
| `created_at` | timestamptz | no | |

`UNIQUE (tender_id, from_snapshot_id, to_snapshot_id, field_key) NULLS NOT
DISTINCT` - makes re-diffing idempotent. `from_snapshot_id` is nullable for the
first snapshot.

---

## 4. Per-company judgement

### `company_tenders`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK, `RESTRICT` |
| `tender_id` | uuid | no | FK, `CASCADE` |
| `saved_at` | timestamptz | yes | |
| `dismissed_at` | timestamptz | yes | |
| `bidding_at` | timestamptz | yes | **the only flag that may auto-fetch documents** |
| `notes` | text | yes | |

`UNIQUE (company_id, tender_id)`. Timestamps, not booleans — you keep *when*.

### `evidence_matches`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `RESTRICT` |
| `requirement_id` | uuid | no | FK `RESTRICT` |
| `company_document_version_id` | uuid | no | FK `RESTRICT`; the exact bytes the human confirmed |
| `status` | text | no | `evidence_status`; `covered` is Pass-eligible, `expired`/`contradicted` are Fail, `missing` is Unknown |
| `reason` | text | yes | why missing/expired/contradicted |
| `confirmed_by` | uuid | yes | FK → users; **human confirmation is required for `covered`** |
| `confirmed_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

`UNIQUE (company_id, requirement_id)`.

**Pinned to a version.** Expiring or soft-deleting the version recomputes the
match to `expired`/`missing` and clears `confirmed_by`; re-confirmation is
required. The parent document is never the reference, because new bytes under
an old confirmation would silently inherit it.

### `company_requirement_flags`

Splits "is this extraction real?" (global) from "do I care?" (per company) — blueprint §6.5.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK |
| `requirement_id` | uuid | no | FK |
| `ignored` | boolean | no | default `false` |
| `note` | text | yes | |

`UNIQUE (company_id, requirement_id)`.

### `fit_assessments`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK |
| `tender_id` | uuid | no | FK `CASCADE` |
| `score` | numeric(5,2) | no | 0–100 |
| `components` | jsonb | no | `{profile_fit: 25, citations: 20, evidence: 40, readiness: 15}` actuals |
| `gate` | text | no | `gate_result` |
| `gate_reason` | text | yes | readable |
| `assessed_at` | timestamptz | no | |

`UNIQUE (company_id, tender_id)`. `CHECK (score >= 0 AND score <= 100)`.

The four components are computed deterministically: `25 × matched profile
terms / total profile terms`, `20 × (verified + accepted_ocr) / active
requirements`, `40 × mandatory criteria with confirmed non-expired evidence /
mandatory criteria`, and `15 × documents present / covers expected` (0 when no
covers are known), each rounded to 2dp.

### `assessment_criteria`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `RESTRICT` |
| `tender_id` | uuid | no | FK `CASCADE` |
| `requirement_id` | uuid | no | FK `RESTRICT` |
| `criterion_text` | text | no | the mandatory criterion being judged |
| `verdict` | text | no | `assessment_verdict` |
| `reason` | text | yes | readable |
| `evidence_match_id` | uuid | yes | FK → evidence_matches, `SET NULL` |
| `is_active` | boolean | no | default `true` |
| `created_at`, `updated_at` | timestamptz | no | |

Partial unique: `(company_id, tender_id, requirement_id) WHERE is_active`.

**Gate aggregate.** Any `fail` -> `fail`; else any `unknown` -> `unknown`; else
`pass`. `accepted_ocr` may back a `pass` only when the match was human-confirmed
and the OCR provenance is recorded; the UI says "Pass - OCR citation,
human-confirmed".

### `checklist_tasks`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK `RESTRICT` |
| `tender_id` | uuid | no | FK `CASCADE` |
| `requirement_id` | uuid | yes | FK `RESTRICT` (a human decision must not vanish) |
| `phase` | text | no | default `pre_award` |
| `title` | text | no | exact document/action |
| `category` | text | yes | copied from the requirement |
| `acceptance_criteria` | text | yes | |
| `issuer` | text | yes | who issues/accepts |
| `signatory` | text | yes | |
| `evidence_check` | text | yes | |
| `due_at` | timestamptz | yes | |
| `trigger` | text | yes | when due is not a date |
| `consequence` | text | yes | **only if the source states it** |
| `document_id` | uuid | yes | copied citation at generation, FK `RESTRICT` |
| `page_index` | integer | yes | copied citation at generation |
| `clause_ref` | text | yes | copied citation at generation |
| `excerpt` | text | yes | copied citation at generation |
| `source_url` | text | yes | copied citation at generation |
| `status` | text | no | `checklist_status` |
| `owner` | text | yes | preserved across regeneration |
| `created_at`, `updated_at` | timestamptz | no | |

`UNIQUE (company_id, tender_id, requirement_id, phase)` - one task per
requirement per phase. The citation is copied, so re-extraction cannot rewrite a
task's evidence. Multiple tasks per requirement wait for a future `task_key`.

Indexes: `(company_id, due_at)`, `(company_id, tender_id)`.

### `reminders`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | no | FK |
| `tender_id` | uuid | yes | FK `CASCADE` |
| `task_id` | uuid | yes | FK `CASCADE` |
| `key` | text | no | deterministic, e.g. `closing:tender:<id>` |
| `due_at` | timestamptz | no | |
| `state` | text | no | `reminder_state` |
| `sent_at` | timestamptz | yes | |
| `acknowledged_at` | timestamptz | yes | |
| `lead_days` | integer | yes | which lead produced this reminder |
| `attempts` | integer | no | default `0` |
| `last_error` | text | yes | readable |
| `created_at`, `updated_at` | timestamptz | no | |

`UNIQUE (company_id, key)` - re-deriving reminders is idempotent.

### `audit_logs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | yes | FK `RESTRICT` |
| `actor_user_id` | uuid | yes | FK → users |
| `action` | text | no | e.g. `requirement.rejected` |
| `entity_type` | text | no | |
| `entity_id` | uuid | yes | |
| `before` | jsonb | yes | |
| `after` | jsonb | yes | |
| `created_at` | timestamptz | no | |

Index: `(company_id, created_at DESC)`.

---

## 5. Work and system

### `jobs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | yes | FK; null for scheduled/system work |
| `kind` | text | no | `job_kind` |
| `lane` | text | yes | `browser` when it needs the browser |
| `state` | text | no | `job_state`, default `queued` |
| `priority` | integer | no | default 100; **lower runs first**; user-initiated < scheduled |
| `payload` | jsonb | no | default `'{}'` |
| `idempotency_key` | text | yes | **UNIQUE** — safe double-enqueue |
| `attempts` | integer | no | default `0` |
| `max_attempts` | integer | no | default `3` |
| `lease_owner` | text | yes | worker id |
| `lease_expires_at` | timestamptz | yes | stale lease ⇒ back to `queued` |
| `run_after` | timestamptz | no | default `now()` |
| `last_error` | text | yes | readable |
| `started_at`, `finished_at` | timestamptz | yes | |
| `created_at`, `updated_at` | timestamptz | no | |

Indexes: `(state, run_after)`, `(lane, state)`, `(company_id, created_at DESC)`.

`CHECK` on state transitions is enforced in code, not in SQL; the constraint only validates membership. A browser job's lane must be held in `lane_leases`; `app_state` is not a lock because it has no expiry.

### `runs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `company_id` | uuid | yes | |
| `job_id` | uuid | yes | FK → jobs `SET NULL` |
| `kind` | text | no | reporting only |
| `status` | text | no | `job_state` (reuse) |
| `metrics` | jsonb | no | default `'{}'` - rows_seen, notices_read, unlinked, unreadable, listed |
| `notes` | text | yes | |
| `started_at`, `finished_at` | timestamptz | yes | |
| `created_at` | timestamptz | no | |

`metrics` is what makes an honest run report possible: a run that read and
discarded must not look like a run that found nothing. The canonical run
resource is the job; reporting rows are addressed at `/api/jobs/{id}/runs`.

### `challenges`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `job_id` | uuid | yes | FK `CASCADE` |
| `run_id` | uuid | yes | FK `CASCADE` |
| `kind` | text | no | `challenge_kind` |
| `image_path` | text | yes | decoded captcha picture — a picture, not a page screenshot |
| `context` | jsonb | no | default `'{}'` — which document was being fetched |
| `state` | text | no | `challenge_state`, default `open` |
| `opened_at` | timestamptz | no | |
| `expires_at` | timestamptz | no | **always set** — an abandoned challenge must not block the lane |
| `answered_at` | timestamptz | yes | |
| `attempts` | integer | no | default `0` |

**No answer column.** The human types into the portal's own page; the software never stores or replays the answer. `POST /challenge` carries no answer; the driver watches the session for the download/navigation event and resumes. A second captcha is reported, never looped on.

### `lane_leases`

| Column | Type | Null | Notes |
|---|---|---|---|
| `name` | text | no | PK; `browser` |
| `owner_kind` | text | no | `job` / `run` |
| `owner_id` | uuid | no | |
| `acquired_at` | timestamptz | no | |
| `lease_expires_at` | timestamptz | no | stale lease is reclaimable |
| `reserved_for_challenge` | boolean | no | default `false`; other browser jobs queue |
| `expires_at` | timestamptz | no | challenge reservation expiry |

### `app_state`

| Column | Type | Null | Notes |
|---|---|---|---|
| `key` | text | no | PK |
| `value` | jsonb | no | |
| `updated_at` | timestamptz | no | |

Known keys: `last_completed_run_at` (global catch-up fact), and later
`active_embedding_collection`.

### `otp_codes`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `user_id` | uuid | yes | FK `CASCADE`; null for a pending signup |
| `email` | citext | no | for signup before the user exists |
| `purpose` | text | no | `otp_purpose` |
| `code_hash` | text | no | HMAC - never the code |
| `payload` | jsonb | no | default `'{}'`; pending signup holds hashed password + company name |
| `expires_at` | timestamptz | no | |
| `consumed_at` | timestamptz | yes | single use |
| `attempts` | integer | no | default `0` |

Index: `(email, purpose, created_at DESC)`. Cleanup job deletes `expires_at <
now() - interval '1 day'` (replaces the Mongo TTL index).

### `discovery_candidates`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | PK |
| `run_id` | uuid | no | FK `CASCADE` |
| `url` | text | no | |
| `kind` | text | no | `candidate_kind`: `source` or `tender_candidate` |
| `outcome` | text | no | `candidate_outcome`: `stored`, `rejected`, `unreadable`, `unlinked` |
| `reason` | text | yes | why rejected/unreadable/unlinked |
| `listing_url` | text | yes | where the candidate was found |
| `first_seen_at` | timestamptz | no | |
| `last_seen_at` | timestamptz | no | |

Index: `(run_id, kind, outcome)`. Rejection reasons are rows, not counts, so
"read and discarded" is provable.

---

## 6. Deferred tables (do **not** create yet)

`chunks`, `answers`, `retrieval_cache`. Shapes are specified in blueprint §5.11 and §6.8. Creating them before the feature exists is waste and invites half-implementations.

---

## 7. Constraint and index summary

| Kind | List |
|---|---|
| **Unique** | `users.email`, `portals.code`, `company_document_versions(company_document_id, sha256)`, `company_document_pages(company_document_version_id, page_index)`, `company_document_terms(company_document_id, normalized)`, `company_capabilities(company_document_id, normalized)`, `company_experiences(company_document_id, normalized)`, `tenders.dedupe_key`, `tenders(portal_id, portal_tender_id)` partial, `tender_locators.tender_id`, `tender_covers(tender_id, version, cover_no)`, `tender_documents(tender_id, sha256)`, `tender_documents(tender_id, document_key, version)`, `document_fetch_state.tender_id`, `document_pages(document_id, page_index)`, `requirements(document_id, page_index, excerpt_hash)` partial where `is_active`, `tender_snapshots(tender_id, captured_at)`, `changes(tender_id, from, to, field_key)` `NULLS NOT DISTINCT`, `company_tenders(company_id, tender_id)`, `evidence_matches(company_id, requirement_id)`, `company_requirement_flags(company_id, requirement_id)`, `fit_assessments(company_id, tender_id)`, `assessment_criteria(company_id, tender_id, requirement_id)` partial where `is_active`, `checklist_tasks(company_id, tender_id, requirement_id, phase)`, `reminders(company_id, key)`, `tender_similarity_judgments(company_id, tender_id)`, `requirement_experience_matches(company_id, requirement_id)`, `jobs.idempotency_key` |
| **Cascade** | tender -> documents, pages, covers, requirements, snapshots, locator, fetch state, changes; company document -> versions; run -> discovery candidates |
| **Restrict** | requirement -> evidence_matches, assessment_criteria, checklist_tasks; company_document_version -> evidence_matches; company -> everything |
| **Set null** | `tenders.supersedes_tender_id`, `tenders.merged_into_tender_id`, `tender_documents.supersedes_document_id`, `runs.job_id`, `challenges.job_id` on job delete is cascade (challenge is child, not history) |
| **Partial** | portal identity, current document per `(tender_id, document_key)`, active requirement excerpt, active assessment criterion, `changes.from_snapshot_id` nullable |

## 8. Stage A migration checklist

1. Extensions: `pgcrypto` (for `gen_random_uuid`), `citext`.
2. Create tables in dependency order: companies -> users -> company_documents -> versions -> company_document_pages -> company_document_terms -> portals -> tenders -> locators -> covers -> documents -> fetch state -> pages -> requirements -> snapshots -> changes -> company_tenders -> flags -> evidence_matches -> assessment_criteria -> fit_assessments -> checklist_tasks -> reminders -> audit_logs -> jobs -> runs -> challenges -> lane_leases -> app_state -> otp_codes -> discovery_candidates.
3. Add every `CHECK` from §1 and every unique constraint from §7.
4. Seed `app_state` with `last_completed_run_at = null` (null means "never run" => catch-up fires on first boot) and one `portals` row for CPPP.
5. Prove it: `alembic upgrade head` on an empty database, then `downgrade base` and up again.
6. Regression tests from guide §9 must pass against the fresh schema.
