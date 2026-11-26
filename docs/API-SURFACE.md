# API surface

**Purpose:** the exact HTTP contract, so the backend and the ported frontend cannot drift. Behaviour lives in the blueprint (Part 5); this file only fixes method, path, shape and status codes.

## Conventions

| Topic | Rule |
|---|---|
| Base path | `/api` - the frontend proxies `/api` to the backend |
| Auth | `Authorization: Bearer <jwt>`; every route except `/api/auth/*` and `/api/health` requires it |
| Tenancy | `company_id` is loaded from the user row; the token's `company_id` claim is display data, never an authorisation source |
| Cross-tenant access | returns **404**, never 403 (do not confirm existence) |
| Errors | `{"detail": "<sentence>"}`; 422 for validation, 409 for state conflicts, 503 for a missing integration |
| Dates | RFC 3339 with offset; date-only values are sent as `YYYY-MM-DD` |
| Money | `{"amount": "840000.00", "currency": "INR"}` - a string, never a float |
| Ids | opaque UUID strings |
| Idempotency | POST endpoints that write are idempotent by natural key (document sha, tender identity, checklist per requirement) |
| Pagination | `?limit=&cursor=` on lists; responses `{"items": [...], "next": "<cursor>|null"}` |
| States | responses carry the actual state (`unverified`, `partial`, `needs_you`), not a bare success |

## Module registration

The base does **not** contain the scraper. It always exposes `/api/health` and the
auth/company/corpus endpoints. Installing the scraping module registers
`/api/discovery/*` and `POST /api/tenders/{id}/documents/fetch`; until then the
health response reports `features.discovery=false` and the frontend hides those
affordances. A missing module is never a 500.

## Auth

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/auth/register` | start a signup, send a code | **202** for a new, pending or already-registered address; never 409, never confirms existence |
| POST | `/api/auth/verify-otp` | confirm a signup or login code | body `{email, code, purpose}` where purpose is `signup` or `login`; single use; signup creates the user/company |
| POST | `/api/auth/resend-otp` | new signup/login code | 202 with the same generic shape; 429 inside cooldown for a live challenge |
| POST | `/api/auth/request-login-code` | start email-code sign-in | 202 whether or not the address is known; sends only for a verified, opted-in account |
| POST | `/api/auth/login` | password login | 401 for a bad password or code-only account; 403 if unverified/inactive |
| GET | `/api/auth/me` | current user + company | |
| PATCH | `/api/auth/security` | change code sign-in or password | confirm with `current_password` or a fresh `code`; `new_password` optional; a code-only user must set a password before disabling code sign-in; audited |

**No enumeration.** Registration and code requests answer with one sentence and
one status code regardless of whether the email exists. The user and company
are created only when the signup code is accepted; the pending challenge is an
`otp_codes` row keyed by email. The JWT carries `sub` and a `company_id` claim,
but the server reloads the user/company from Postgres on every request.

## Company

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/company` | profile (sectors, services, keywords, region, reminder settings) | |
| PUT | `/api/company` | replace the editable profile | refuses an empty term set when discovery is installed and needs one |
| PATCH | `/api/company` | partial profile / reminder settings | same validation as PUT |
| GET | `/api/company/documents` | evidence library | `{"items": [...]}`; each item carries `extraction_state`, `extraction_error`, `extracted_at`, `term_count` |
| POST | `/api/company/documents` | upload (multipart); sha256 dedupe | PDF/PNG/JPEG; a repeat returns the existing row; an `about_company` PDF enqueues `extract_company_document` (idempotent per document) |
| PATCH | `/api/company/documents/{id}` | type, expiry, issuer, signatory, value | company-scoped; changing the type to `about_company` on a PDF enqueues a read |
| DELETE | `/api/company/documents/{id}` | soft delete | company-scoped; versions stay for audit; terms stop counting at once |
| GET | `/api/company/documents/{id}/download` | the stored file | authenticated, company-scoped |
| POST | `/api/company/documents/{id}/extract` | read an about-company profile again | **202** `{job_id, document_id}`; sets `extraction_state=pending`; only `about_company` PDFs; **400** otherwise |
| GET | `/api/company/documents/{id}/terms` | work phrases/words read from the profile | `{items, extraction_state, extraction_error, extracted_at, term_count}`; each item has `term`, `kind` (`phrase`/`token`), `page_index`, `printed_label`, `excerpt`, `source` |
| DELETE | `/api/company/documents/{id}/terms/{term_id}` | remove one term | **204**; changes suggestions only, never eligibility |
| GET | `/api/company/understanding` | confirmed and proposed kinds of work and delivered projects, with citations | `{documents, capabilities, experiences, counts, rules_version}`; each item carries `page_index`, `printed_label`, `excerpt`, `quote_verified`, `is_ocr`, `state` (`proposed`/`confirmed`/`rejected`) and `origin` (`ai`/`human`); `documents` reports each profile's `understanding_state` and its capability/experience counts |
| PATCH | `/api/company/capabilities/{id}` | confirm, reject or edit one capability | body `{state?, name?, description?, aliases?}`; human edits set `origin=human`; rejected/proposed rows stay visible but are never used |
| PATCH | `/api/company/experiences/{id}` | confirm, reject or edit one delivered project | body `{state?, title?, client?, work_family?, description?}` |
| GET | `/api/company/brief` | the current editable brief, or a deterministic preview before one is saved | `{brief, preview?, terms, rules_version}`; `brief` is null before the first save, and `preview.payload` has the same shape as a stored payload; `brief.source_kind` is `human`, `generated` or `derived` |
| POST | `/api/company/brief` | save an edited brief as a new version | body `{payload}`; increments `version`, makes the new row current and audits the save; the section a term is typed in is its role, and a support/generic phrase put under core is deterministically downgraded; citations on untouched terms are preserved by the client payload |
| POST | `/api/company/brief/generate` | derive a new version from the profile and confirmed understanding | deterministic; never deletes a human version; writes the next version and makes it current; returns the same `{brief, terms, rules_version}` shape |
| GET | `/api/company/brief/versions` | version history, newest first | `{items, next}`; each item carries `id`, `version`, `is_current`, `source_kind`, `summary`, `counts` (core/adjacent/support/generic/projects/certifications) and `created_at` |
| POST | `/api/company/brief/versions/{id}/activate` | make an older version current | company-scoped; **404** for another company's id; audited |

Company-document terms are a matching signal, not eligibility evidence. The
reader keeps repeated multi-word phrases and the specific words inside them,
each with the page and verbatim line it came from. Client/empanelment names,
contact lines and CIN/PAN/GST identifiers are removed before counting. Two
document terms can stand alone; a single one needs the same region rule a bare
keyword needs. Removing a term or deleting/reclassifying the document changes
the company's suggestions and the verdict input hash on the next read.

An `about_company` PDF is also read for **company understanding**: a capability
(a kind of work) and a delivered experience (a named project or product), each
with the page and verbatim line it came from. The reader validates every quote
against that page before storing it, so an invented item is dropped rather than
shown. Only a human-confirmed item is used to compare tenders with the company's
work; a proposed item is shown on the Documents page with its citation and
waits. The response never says whether the company is eligible: eligibility
stays the deterministic gate, and a capability can only help a work-family
comparison and a requirement's "have we done this before" badge.

The **company brief** is the editable answer to "what should discovery treat as
this company's work". It groups the same words into four roles:

| Role | Meaning | Ranking effect |
|---|---|---|
| `core` | a confirmed capability or a delivered project work family | can produce the strongest matches |
| `adjacent` | a specific service or sector the company declared but has not confirmed from a document | ranks below core, above support |
| `support` | delivery words such as `technical support`, `document retrieval`, `version control` | never ranks as core work |
| `generic` | recall words such as `technology`, `web pages`, `software` | used to find notices, never to rank one on its own |

Projects are the strongest fit evidence. Certifications are kept for
eligibility evidence and never make a tender the company's kind of work. A
brief save is a new version, so an older judgment still names the words it used.
A generated brief is refreshed after a capability/experience confirmation or
after an `about_company` document is deleted; a human-edited brief is never
overwritten.

## Discovery (scraping module)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/discovery/status` | last run, last completed at, config caps | powers "scraped 6 min ago"; caps explain a short run; the embedded `sweep` object is the nightly sweep's status (see below) |
| POST | `/api/discovery/runs` | **start a run** | **202** immediately with `{job, created}`; one live run per company; the crawl scope is the union of active profiles, the corpus is global; **409** while a sweep holds the browser |
| GET | `/api/discovery/runs/{id}` | progress: counters + state | thin alias for `GET /api/jobs/{id}` filtered to `kind='scrape_portal'` |
| POST | `/api/discovery/runs/{id}/cancel` | stop cleanly at a step boundary | |
| GET | `/api/discovery/runs` | history | |
| POST | `/api/discovery/sweeps` | **start the whole-portal listing sweep** | **202** with `{job, created}`; reads the directory and every listing page, one organisation at a time, staleness order, commits per page; needs no profile; **409** while run discovery is live; one live sweep per company |
| GET | `/api/discovery/sweeps` | sweep history | newest first |
| GET | `/api/discovery/sweeps/{id}` | one sweep's state | never `needs_you`: a challenge stops a sweep and it keeps what it read |
| POST | `/api/discovery/sweeps/{id}/cancel` | stop at the next page boundary | every page already committed is kept; cancelling is progress, not a rollback |
| POST | `/api/discovery/classify` | queue the labelling + verdict pass | **202** with `{job, created}`; idempotent per row (`rules_version` + content hash); safe to call at any time; a second request returns the queued job |
| POST | `/api/discovery/similarity` | queue the closed-label work comparison | **202** with `{job, created}`; body `{tender_ids}` (bounded) or empty for the first page; only tenders this company has a verdict for are judged; the model is never shown the rules tier or bucket; a second request returns the active job instead of stacking; **422** when there is nothing visible or no usable brief/capability term |
| GET | `/api/discovery/sources` | pages stored as `discovery_candidates.kind='source'` | URLs and last-seen times; never listed as tenders |
| GET | `/api/jobs/{id}/runs` | reporting rows for a job | canonical run resource is the job |

The sweep's `GET /api/discovery/status` object is
`{last_sweep, last_summary, due, caps}`. `last_summary` is what the worker
reported (`state`, `metrics` with `pages`, `rows_seen`,
`organisations_walked`, `stored`, `updated`, `rejected`, and `notes`), so a page
can say "read 41 list pages, stored 96 new" instead of showing only a state.
Only a fully `done` sweep resets the nightly clock; a `partial` (a challenge, an
unreadable organisation, a cancellation) does not, so the next scheduler tick
tries again.

## Tenders

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/tenders` | listed tenders | per-company relevance at read time; each item carries `relevance` (`score`, `matched_terms`, `reasons`, `region_match`, plus `bucket` (`strong`/`possible`/`hidden`), `match_score` and `match_reasons` from the same verdict the gate used); query `closing_after`, `saved`, `bidding`, `mine`, `include_dismissed`, `include_hidden`, `q` (server-side contains search over title, reference, authority, organisation chain, location and portal id; `%` and `_` are literal), `sort` (`created_desc` default, `created_asc`, `closing_asc`, `closing_desc`, `match_desc`; null deadlines last; unknown value is a 422), `limit` (1-200, default 50) and `offset`; the response is `{items, total, page, pages, next, profile_complete}` where `total` counts every visible row and `pages` is derived from `limit`; dismissed rows are hidden by default; a saved or bidding tender stays visible even if the profile later changes; `include_hidden=true` is the "show everything" view - the rows the gate classified `hidden` come back labelled with that bucket instead of being dropped, and `total` counts them; `profile_complete` is false when the company declares no services, sectors or keywords (a region alone is not a work profile) and the list is then the whole corpus rather than a profile match |
| POST | `/api/tenders` | create a tender from its first PDF (manual fallback) | multipart `file` + `work_title`; 201 when created, 200 and the existing row when the same bytes and title already exist |
| GET | `/api/tenders/{id}` | one tender + counters + fetch state | |
| POST | `/api/tenders/{id}/save` | creates `company_tenders.saved_at` and a `relevant` feedback row | |
| DELETE | `/api/tenders/{id}/save` | removes the bookmark: clears `saved_at` and deletes this tender's feedback row | idempotent (200 when nothing was saved); does not dismiss, so the tender returns to the gate's view and can be saved again; a bid in preparation keeps the row in My tenders and the response says `bidding: true`; `not-relevant` is the action that removes it from both lists and records the negative weight |
| POST | `/api/tenders/{id}/dismiss` | sets `dismissed_at` | per company only |
| POST | `/api/tenders/{id}/bidding` | sets `bidding_at`; may auto-enqueue a fetch | |
| POST | `/api/tenders/{id}/not-relevant` | record "not relevant" feedback | small negative weight, per company: the matched work terms are demoted on later reads, never banned; repeated marks of the same term do not stack; the verdict cache is invalidated by the feedback hash |
| GET | `/api/tenders/{id}/similarity` | the cached work-comparison answer | `{tender_id, similarity}`; `similarity` is null before the tender is judged, otherwise `{answer, title, anchor_text, anchor_verified, tender_evidence, company_evidence, match_type, evidence_rank, needs_confirmation, reason, capabilities, judged_at, model, rules_version}`; `title` is the notice title the model saw, and both evidence fields are the exact stored lines, never paraphrases |
| POST | `/api/tenders/{id}/similarity` | judge one tender on demand | same payload; one bounded model call; `force=true` re-asks; **422** only when neither a confirmed capability nor a current brief term exists |
| GET | `/api/tenders/{id}/requirement-experience` | what the company's own experience shows for each requirement | `{items, counts, rules_version}`; `items` carry `requirement_id`, `coverage` (`covered`/`partial`/`new`/`unknown`), `capabilities`, `experiences`, `company_citation` (`document_id`, `page_index`, `excerpt`) and `reason`; `counts` repeats the four buckets |
| POST | `/api/tenders/{id}/requirement-experience` | compare requirements with confirmed experience | one bounded, user-triggered call; unchanged requirements reuse their stored mapping; no confirmed item -> **422**, no AI key -> **503**; returns the same listing |
| POST | `/api/tenders/{id}/refresh` | enqueue a source recheck | 202 with job id; when the portal states the current bidding cycle is terminated, the job records it and returns the fixed message instead of updating the notice fields |
| POST | `/api/tenders/{id}/lineage/{candidate_id}/confirm` | confirm a proposed corrigendum/replacement link | audited |
| POST | `/api/tenders/{id}/lineage/{candidate_id}/reject` | reject a proposed link | audited |

`TenderOut` carries `cycle_state` (`active`/`terminated`), `termination_excerpt`
(the portal's verbatim sentence) and `termination_observed_at`. It also carries
`saved` and `bidding`, read from the company's own `company_tenders` row, so a
card can offer "remove from saved" only when the tender is actually saved
instead of guessing. A terminated
tender keeps its identity for routing but the interface shows only the
terminated-cycle message: no authority, reference, value, deadline, documents or
tabs. The state is never set from a missing notice or a passed deadline; only an
explicit statement the app read on the portal page sets it.

With no scraping module installed, `POST /api/tenders` is the only way to
create a tender. It parses the PDF before committing, so a corrupt file never
leaves an empty tender behind.

### Relevance roles, rungs, verdicts and audit

The corpus is global and the profile is per company, so the listing decision is
made at read time and is never a storage filter. A company's words are split
into four roles before any matching happens:

| Role | Meaning |
|---|---|
| `core` | a confirmed capability or a delivered project's work family |
| `adjacent` | a specific service or sector the company declared but has not confirmed from a document |
| `support` | delivery words such as `technical support`, `document retrieval`, `version control` |
| `generic` | recall words such as `technology`, `web pages`, `software` |

Those roles produce seven rungs:

| Tier | Evidence | Bucket |
|---|---|---|
| 1 | a full multi-word core phrase, or a core match plus adjacent/support evidence | `strong` |
| 2 | a core match alone or a partial core token | `strong` |
| 3 | adjacent declared work plus a region match | `strong` |
| 4 | adjacent declared work alone | `possible` |
| 5 | a support term plus a region match | `possible` |
| 6 | a support term alone, or a generic-only match | `possible` |
| 7 | no usable match | `hidden` |

A whole phrase counts two units and a single term or partial token one; a tier
is the rung, and the score is only a band inside it (`90-95`, `80-88`, `70-78`,
`60-68`, `50-58`, `40-48`, `0-25`, capped at 95) so the interface never reads
100%. Generic words are kept for **recall**, not ranking: a generic-only match
is visible at tier 6 and never lifts itself above a support or adjacent match.
Fit reads only the declared words and the notice: there is no corpus-rarity
pass, so a page request never scans the whole corpus for an explanation. The
authority and organisation chain are never work evidence; the region only breaks
the tie at tiers 3 and 5. A tier says nothing about eligibility.

"Not relevant" feedback demotes the matched terms by `0.15` per rejected term,
clamped to tier 4-6: it never bans a tender and repeats of the same term do not
stack.

`sort=match_desc` is the fit order the interface defaults to: every visible row
is ordered by its `relevance.match_score` descending, and rows with the same
score by `created_at` descending. Because fit is a read-time verdict rather
than a column, the ordering is applied in Python over the page's own gate
result; the SQL column orders stay available for a reader who asks for them.
A stronger fit read last week therefore leads a weaker fit read today.

Per-company verdicts are stored in `company_tender_verdicts` keyed by
`verdict_input_hash(profile_hash, tender_content_hash, feedback_hash,
rules_version)`, so a read reuses a verdict only while the profile, the tender,
the feedback and the rules are unchanged. `tender_content_hash` is one shared
function used by both the read path and the background pass. Deadlines and
dismissals stay live filters and are not part of the cached key.

Every sighting is recorded in `tender_observations` (append-only: the sequence
is the evidence, so "present here on Monday, absent on Tuesday" is a fact), and
`discovery_candidates` records every URL a run or sweep saw with why it was
stored or rejected.

### Work understanding and the closed-label comparison

The rules gate decides most of the corpus. Every candidate that reaches the AI
is judged **without being told its rules tier**, so the model cannot simply echo
the gate. It may answer only `yes`, `no` or `not_sure` for each notice — no
score, no free-form reasoning, no eligibility call, no invented capability. The
company side is the current brief (or its deterministic preview), not just the
confirmed capabilities, so a declared adjacent term can still be compared.

Each answer is evidence-aware:

| Field | Rule |
|---|---|
| `title` | the notice title the model saw, stored as `judged_title` |
| `tender_evidence` | an exact line from the notice's title, work type or cited requirements |
| `company_evidence` | an exact line from the brief or its cited document lines |
| `match_type` | `exact_core`, `semantic_core`, `adjacent`, `support_only`, `generic_only` or `no_evidence` |
| `reason` | one short sentence; never eligibility, money or a bid decision |

Both quotes are validated against the stored lines before they are shown. A
`yes` whose quotes cannot be found is stored as `unsure` with
`needs_confirmation`, and it changes no score; the interface says "AI suggested
— confirm". A missing side is a valid `no_evidence` answer, not an invitation to
paraphrase.

The evidence hierarchy is deterministic and comes from the matched company
line's role, not from model confidence:

| Match type | Rank | Ranking effect |
|---|---|---|
| `exact_core` | 1 | tier 1 |
| `semantic_core` | 2 | tier 2 |
| `adjacent` | 3 | tier 3 with the company's region, otherwise tier 4 |
| `support_only` | 4 | tier 5 with the region, otherwise tier 6 |
| `generic_only` | 5 | tier 6 |
| `no_evidence` | 6 | no adjustment |

A `no` is stored as tier 6 at the bottom of the possible band and appends a
reason; it never hides, deletes or dismisses the tender. The adjustment is
applied on the **read path** on top of the cached rules verdict, so
`company_tender_verdicts` stays rules-only and the comparison never changes an
eligibility answer. Judgments are cached per company/tender by an input hash
covering the tender's content hash, the confirmed capability signature, the
brief payload signature and the rules version; a repeat read does not re-ask
while a brief edit, a capability edit or a re-published notice does.

Requirement coverage is a separate, user-triggered comparison: `covered` and
`partial` carry the page and verbatim line from the company's own document,
`new` means nothing confirmed shows that work (a prompt to look, not a failure)
and `unknown` means the requirement is not work-bearing or the reader could not
place it. A requirement the reader skips is `unknown`, never `new`. When a
checklist task is generated for a `new` requirement, its evidence check says so;
the citation itself is copied from stored rows, never from model output.

## Documents

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/tenders/{id}/documents` | documents + covers + fetch state | reflects **global** state |
| POST | `/api/tenders/{id}/documents` | manual upload (fallback path) | one PDF; sha256 dedupe |
| POST | `/api/tenders/{id}/documents/archive` | manual zip upload | each PDF inside becomes its own document; non-PDF members are reported in `skipped` |
| POST | `/api/tenders/{id}/documents/fetch` | **enqueue a fetch job** | module route; 202 with job id; never blocks the request |
| GET | `/api/tender-documents/{id}` | metadata + citation counts | |
| GET | `/api/tender-documents/{id}/pages` | page index + labels | |
| GET | `/api/tender-documents/{id}/pages/{page}` | one page's text | `is_ocr` flag included |
| GET | `/api/tender-documents/{id}/download` | the file | authenticated |
| POST | `/api/tender-documents/{id}/extract` | enqueue requirement extraction | `replace=true` sets AI-origin requirements `is_active=false`; it never deletes them |

**There is no `DELETE /api/tender-documents/{id}`.** Documents are global; a
wrong file is marked invalid by the reconciler, never removed by a user.

## Requirements

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/tenders/{id}/requirements` | list with state, citation, category | filters by state/category; navigation hits hidden by default |
| GET | `/api/tenders/{id}/requirements/summary` | counts per state | |
| GET | `/api/requirements/{id}` | one requirement + full citation | |
| PATCH | `/api/requirements/{id}` | human state change | **cannot** set `verified`; may set `needs_review`, `rejected`, `accepted_ocr`; audited with the actor |
| POST | `/api/tenders/{id}/requirements` | add a human-authored requirement | `origin='human'`; audited |
| POST | `/api/requirements/{id}/flag` | per-company ignore/note | does not affect other companies |
| PATCH | `/api/requirements/{id}/flag` | update the per-company flag | does not affect other companies |

**Global annotation is advisory.** A `rejected` requirement stays visible to
everyone, badged "flagged by another user"; a company hides it for itself with
`company_requirement_flags`. No role can erase corpus evidence.

## Assessment

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/tenders/{id}/assessment` | score + components + gate | gate rendered first by the UI |
| POST | `/api/tenders/{id}/assessment` | recompute | deterministic, no AI |
| GET | `/api/tenders/{id}/requirements/{rid}/evidence` | candidate matches | suggestions only |
| POST | `/api/tenders/{id}/requirements/{rid}/evidence` | confirm/link evidence | human confirmation required for `covered`; pinned to a document version |
| GET | `/api/tenders/{id}/legal` | cited legal/contract obligations | post-award kept separate |
| GET | `/api/tenders/{id}/checklist` | tasks | optional `phase=pre_award|post_award` |
| POST | `/api/tenders/{id}/checklist/generate` | idempotent generation | preserves status/owner; copies the citation at generation |
| GET | `/api/checklist` | all company-scoped tasks | query `phase`, repeated `status`, `limit` up to 2000 |
| POST | `/api/checklist` | add a manual task | company-scoped |
| PATCH | `/api/checklist/{task_id}` | status/owner/due | audited |

The gate is `pass | fail | unknown` and is computed only from mandatory
requirements whose category is `eligibility`. Evidence candidates are
suggestions; `POST .../evidence` accepts `covered` (pins the latest, or a named,
company document version), `contradicted` (a human-confirmed failure) and
`missing` (clears the confirmation). An expired, rejected or removed document
recomputes to `expired`, clears the confirmation and can never pass. `accepted_ocr`
may pass only after a human accepts the OCR citation, and the response marks it
with `ocr_confirmed=true`.

Checklist generation creates both phases in one call and separates them by
`phase` (`pre_award` tasks, `post_award` obligations). It copies the citation
(`document_id`, `page_index`, `clause_ref`, `excerpt`, `source_url`) at
generation and never overwrites an existing task's status, owner or due date.

## Jobs (the shared background contract)

Used by discovery, document fetch, extraction and snapshots alike. The scraping
module contributes endpoints only through the ones listed here - it does not add
its own job contract.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/jobs` | list, filter by kind/state | |
| GET | `/api/jobs/{id}` | state, attempts, error, progress metrics | canonical run resource |
| POST | `/api/jobs/{id}/cancel` | cancel at a step boundary | |
| POST | `/api/jobs/{id}/retry` | re-enqueue a failed job | new idempotency key |
| GET | `/api/jobs/needs-you` | everything waiting on a human | the "you have 2 prompts" badge |
| POST | `/api/jobs/{id}/challenge` | **signal that the human has answered elsewhere** | body carries no answer; the human types into the portal page |
| POST | `/api/jobs/{id}/challenge/answer` | **carry the human's answer into the portal once** | body `{answer}`; used once and dropped, never stored, logged or echoed; returns `{accepted, job}` |
| GET | `/api/jobs/{id}/captures/{file}` | screenshots/captcha images for support | authenticated, path-sanitised |
| GET | `/api/jobs/{id}/runs` | reporting rows for one job | `{items, next}`; counters live on each run |

**Shape.** Lists return `{items, next}`. A job carries `id`, `company_id`,
`kind`, `lane`, `state`, `priority`, `payload`, `attempts`, `max_attempts`,
`last_error`, `run_after`, `started_at`, `finished_at`, `created_at`,
`updated_at` and, when one exists, the latest `challenge`
(`id`, `kind`, `image_url`, `context`, `state`, `opened_at`, `expires_at`,
`answered_at`). A `challenge` never carries an answer; `context` may carry
`instructions`, `document`, `screenshot_url` (the full page) and
`answer_rejected`.

**Visibility.** A system job has `company_id = null` and is visible to every
authenticated company (discovery is global). A company job is visible only to
its company; another company gets `404`, never `403`.

**Status codes.** `cancel` on a running, queued or `needs_you` job returns the
cancelled job; cancelling a terminal job is `409`. `retry` accepts `failed`,
`partial` or `cancelled` and clears the idempotency key; anything else is `409`.
`POST /challenge` and `POST /challenge/answer` on a job that is not `needs_you`
are `409`, and an expired challenge is `409` with the job recorded `partial`.
An answer to a job whose browser session is gone is `409` with a restart note;
an empty or over-long answer is `422`.

**Challenge semantics.** A `needs_you` job releases its worker thread and holds
the browser session under a bounded lease (`AGENT_CHALLENGE_WAIT_SECONDS`,
default 600). The lane is marked `reserved_for_challenge`, so other browser jobs
queue while non-browser jobs continue. `POST /challenge/answer` types the
human's characters into the portal's own field and submits with the portal's
control, then drops them; the answer never reaches a column, a log line, a
metric or the response body. If the portal rejects it, the job stays parked, the
challenge keeps the same file request and `image_url`/`screenshot_url` are
refreshed, and `accepted` is `false`; a per-link retry budget bounds this.
If the portal accepts it, the job reads the response page for its own file or
download link and never requests the original captcha URL again; a page that
hands over no file marks that link done with a clear error, and one fetch shows
at most six pictures before reporting the rest. `POST /challenge` remains the
signal-only path (a human who acted outside the app, or a non-browser challenge
such as OTP). If the lease expires, the session is dropped and the job records
`partial` with "file request expired". There is no `CAPTCHA_MODE` and no stored
answer.

**Cancel semantics.** `POST /api/jobs/{id}/cancel` and
`POST /api/discovery/runs/{id}/cancel` set the job `cancelled`, expire its open
challenge and close its browser session. A running walk or fetch checks the row
on its next heartbeat and raises `JobCancelled`, so it stops at that step rather
than finishing the walk or the remaining files; a `needs_you` job is closed
immediately.

## Change review

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tenders/{id}/changes` | diffs between snapshots |
| GET | `/api/tenders/{id}/snapshots` | history, newest first, id tie-break |
| POST | `/api/tenders/{id}/snapshot` | enqueue a snapshot capture |
| GET | `/api/changes` | global change feed across saved tenders |
| PATCH | `/api/changes/{id}` | acknowledge or dismiss (`review_state`) |
| GET | `/api/tenders/{id}/reminders` | derived reminders |
| GET | `/api/reminders` | all reminders |
| POST | `/api/reminders/{id}/acknowledge` | record human acknowledgement |
| POST | `/api/reminders/{id}/dismiss` | dismiss |
| GET | `/api/calendar/tender/{id}.ics` | authenticated ICS |
| GET | `/api/calendar/tasks.ics` | authenticated ICS |
| POST | `/api/tenders/{id}/lineage/propose` | propose a corrigendum link from matching identity fields |
| POST | `/api/tenders/{id}/lineage/{candidate_id}/confirm` | human confirms the proposed link |
| POST | `/api/tenders/{id}/lineage/{candidate_id}/reject` | human rejects the proposed link |

Snapshots and changes belong to the global corpus; a snapshot has no
`company_id`. `POST /api/tenders/{id}/snapshot` returns `202` with a
`snapshot_tender` job, and the handler captures a new snapshot only when the
content hash differs. Capture times are strictly increasing per tender,
stepped by a millisecond when the clock is coarse, and the history sorts by
`(captured_at DESC, id DESC)`. A change is the deterministic diff between two
snapshots. Requirement review state (`needs_review`, `accepted_ocr`, `rejected`)
is stored in the fingerprint but excluded from the content hash and the diff:
a human review is not a tender change. The `field_key` is the tender field or
`req:{requirement_id}`.

The global `/api/changes` feed is filtered to the company's saved,
non-dismissed tenders (pass `saved_only=false` to widen it). Review is an
advisory, audited annotation on the global change (`pending`, `acknowledged`,
`dismissed`), not a per-company hidden row. `lineage` is proposed only when
portal, normalised reference, organisation, work title and a later publication
date all agree; a shared reference alone is never enough. Confirmation is
always human.

Reminders are company-scoped and derived from saved/bidding tender deadlines
and task due dates. A moved due date reopens the reminder and clears the old
acknowledgement; completing or removing the source removes it. `email_enabled`
reflects the company's opt-in and `email_configured` reports whether SMTP is
set. Live email is sent by the cron-able
`backend_v2/scripts/send-reminders.py` job (dry-run by default, exit `2`
without `SMTP_APP_PASSWORD`).

## Audit, health and support

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/audit` | company-scoped audit trail | filter by entity/action |
| GET | `/api/health` | liveness + dependency status | DB always; `schema` reports `revision`, `head`, `up_to_date` and a database behind head is `degraded`; `features.discovery` tells the frontend what is installed |
| GET | `/api/health/integrity` | drift + citation re-validation results | reports, does not fix |

A server refuses to start when the database is stamped behind the migration
head (`SCHEMA_CHECK=true`, the default): the message names both revisions and
the `alembic upgrade head` command, because every request against a stale
schema would otherwise fail one at a time with an undefined table. An
unreachable database still bootsâ€”`/api/health` reports `database: down`, so
liveness is never confused with readiness.

## Status codes, decided once

| Situation | Code |
|---|---|
| Created | 201 |
| Accepted, work queued | 202 + job id |
| Bad input | 422 |
| Not yours / not found | 404 |
| State conflict (already fetched, already answered, expired challenge) | 409 |
| Missing integration (no SMTP, no AI key) | 503 |
| Rate-limited (OTP) | 429 |
| Register / request code, known or unknown email | **202**, same body |

**Never** return 200 with an empty list to mean "it failed". A failed fetch is a
202 job that later reports `failed` or `partial` with a readable reason.

