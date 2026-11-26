# BidPilot AI — System Blueprint (from zero)

**Status:** design document. It describes the intended end-state system, starting from nothing, and explains every decision in it. Status markers used throughout:

| Marker | Meaning |
|---|---|
| **[BUILT]** | exists in the current tree (before the rebuild) |
| **[PLANNED]** | decided, not built |
| **[DEFERRED]** | decided but deliberately out of the near-term build |
| *(no marker)* | design decision; neither built nor scheduled |

**Audience:** anyone — a developer, a reviewer, a new team member, or an AI agent picking this up cold. No prior knowledge of tender portals, web scraping, or vector databases is assumed. Every term is defined the first time it is used and collected again in the [Glossary](#part-20-glossary).

**How to read it:** Part 0 to Part 4 explain *what* the system is and *why*. Part 5 onward explain *how*, component by component. Part 8 has end-to-end timelines — if you only read two sections, read Part 3 and Part 8.

**This system is being rebuilt from scratch.** Parts 16–19 exist specifically for that: what we receive, what we must build, what must be deleted, the interfaces the scraping layer will plug into later, and the hard-won traps that must not be repeated. If you are starting the rewrite today, read **Part 16 and Part 17 before writing any code** — they decide whether the scraping layer can be added later without surgery.

**This document is one of five, and the rebuild happens in a separate code tree.** Nothing an implementer needs lives only in a conversation. The set is indexed in [`README.md`](./README.md); the column-level schema is in [`SCHEMA-SPEC.md`](./SCHEMA-SPEC.md), the HTTP contract in [`API-SURFACE.md`](./API-SURFACE.md), and the reasoning trail (non-authoritative) in [`archive/DISCUSSION-TRANSCRIPT.md`](./archive/DISCUSSION-TRANSCRIPT.md).

---

# Part 0 — The problem, in plain language

A company that wants government work in India must find "tenders" (public invitations to bid, also called notices) and then prove it is allowed to bid.

Today that is painful for four separate reasons:

1. **Finding them is manual.** Tenders are scattered across portal websites that look like they were built in 2006 and cannot be reasoned with programmatically without a lot of care.
2. **The useful data is buried.** A portal's front page is a directory. The directory links to a department. The department links to a list of notices. The list links to a notice. The notice links to a set of PDFs. The actual obligations are inside those PDFs. Nothing you want is on the page you landed on.
3. **The PDFs are deliberately awkward to get.** Many portals require a human to answer a captcha before they will hand over a document, and they tie the document to the browser session that requested it. A naive download script gets nothing.
4. **Getting it wrong is expensive.** A missed mandatory requirement, or a misread deadline, means a rejected bid or a wasted bid bond. So the system must be *provably* right, or explicitly say it does not know.

BidPilot AI is a **decision-support** tool for steps 1–4. It discovers tenders, collects the documents, extracts the obligations with citations, compares them to the company's own records, and produces a fit assessment and a compliance checklist.

**It never claims legal eligibility and it never submits a bid.** That boundary is deliberate and is repeated throughout this document, because it is what keeps the product honest.

---

# Part 1 — Vocabulary

Read this once. Everything later depends on it.

| Term | Meaning |
|---|---|
| **Tender / notice** | One public invitation to bid. One tender is one record. |
| **Tender id** | The portal's own identifier for a tender, e.g. `2026_NHAI_295179_1`. Stable and unique — this is how we find a tender again later. |
| **Reference number** | A human-facing tender reference, e.g. `NHAI/HQ/2026/114`. Similar to the tender id but not guaranteed unique. |
| **Organisation chain** | The hierarchy a tender belongs to, e.g. `Ministry of Road Transport → NHAI → Regional Office, Pune`. Used for context, never as proof. |
| **Closing date** | The deadline for submitting a bid. Distinct from the *publish* date and the *opening* date. Confusing these tells a user they have days they do not have. |
| **Captcha** | An image challenge a human must answer to prove they are not a bot. |
| **Session** | A browser's logged-in, cookie-carrying identity with a website. Some portals bind a document to the session that asked for it. |
| **Headless / headed browser** | Headless = browser with no visible window (used on servers). Headed = a real visible window a person can see and click. |
| **CDP** | Chrome DevTools Protocol — the standard way to control a real Chrome from code. |
| **Deterministic** | Computed by plain rules, so the same input always gives the same output. Opposed to "whatever the AI guessed". |
| **Citation** | The proof for a claim: which document, which page, and the exact words quoted. |
| **Verbatim** | Word-for-word, unaltered. |
| **Chunk** | A small slice of a document (a few paragraphs) that is stored and searched as one unit. |
| **Embedding / vector** | A list of numbers representing the *meaning* of text. Similar meanings produce nearby numbers. |
| **Cosine similarity** | The measure of how close two vectors point. High = similar meaning. |
| **Vector database** | A store optimised for "find me the nearest vectors". We use **Qdrant**. |
| **RAG** | Retrieval-Augmented Generation — retrieve the right passages first, then let a model compose an answer *only* from them. |
| **Postgres** | A relational database: tables, rows, strict types, foreign keys, transactions. |
| **Transaction** | A group of database writes that either all succeed or all fail. Nothing half-written. |
| **Job** | A unit of background work with a stored state (queued, running, needs-you, done, partial, failed) that survives an app restart. |
| **Locator** | Our own record of *how to find* a tender again (portal + tender id + org chain). Not a URL. |
| **Daemon thread** | A background worker that does not keep the program alive when the program exits. |

---

# Part 2 — The ten rules that never bend

These are the constitution. Any design decision that violates one of them is wrong, no matter how convenient.

1. **No fabricated citations.** Every requirement shown to a user carries: source document, page (viewer page *and* printed page label if they differ), clause/form reference where available, the exact quoted text, and the source URL plus retrieval time.
2. **If it cannot be verified, say so.** Mark it `unverified`, `partial`, or `conflicting`. Never silently promote a guess to a fact.
3. **Eligibility is not a score.** Mandatory eligibility is `Pass` / `Fail` / `Unknown`, kept strictly separate from the soft fit score. A high score must never hide a failed mandatory criterion.
4. **Missing evidence is never a pass.** Absence of proof is `Unknown`.
5. **Never solve a captcha, an OTP, or a login for the user.** Detect it, show it, let the human answer, then continue. The human is the only thing that can pass it, by design.
6. **The model's output is untrusted.** AI writes drafts. Deterministic code decides what is stored, what is cited, and what is scored.
7. **Checklist items must be tender-specific.** Exact document or action, acceptance criteria, issuer, evidence check, deadline or trigger, and a consequence *only if the source states one*. No "check legal requirements" filler.
8. **Do not assume standard requirements.** Notarisation, stamp paper, affidavits, licence validity — extract each one from *this* tender or do not mention it.
9. **Scan only what can be cited.** Only government sources (`.gov`, `.nic`) are used to answer questions, and only with citations. Anything else is out of scope for answers.
10. **The product supports decisions; it does not make them.** No autonomous bid submission, no claim of legal eligibility.

---

# Part 3 — The three-store architecture (the core idea)

Everything in this document follows from one split.

## 3.1 The split

```
┌─────────────────────────────────────────────────────────────┐
│  POSTGRES  — the source of truth                             │
│  Companies, users, tenders, documents, page text, chunks,    │
│  requirements, evidence, checklists, snapshots, changes,     │
│  jobs, answers, caches.                                      │
│  If it is lost, the truth is lost.                           │
└─────────────────────────────────────────────────────────────┘
                    │                │
     derives from   │                │  derives from
                    ▼                ▼
┌────────────────────────────┐  ┌─────────────────────────────┐
│  QDRANT — derived recall   │  │  GROQ — phrasing            │
│  Vectors + ids + thin      │  │  Writes sentences from      │
│  metadata. Disposable.     │  │  retrieved passages only.   │
│  Rebuildable from Postgres.│  │  Never decides what is true.│
└────────────────────────────┘  └─────────────────────────────┘
```

## 3.2 Why each exists

**Postgres — because this app is mostly *relationships with rules*, not loose documents.**

Consider one tender: it belongs to a company, has documents, each document has pages, each page has chunks, requirements point at a page, evidence points at a requirement, a checklist item points at a requirement, and a snapshot captures all of it at a moment. Those are relations. Deleting a document must not leave a requirement pointing at nothing.

And crucially: the expensive work happens in **background jobs that can die mid-way** (a scrape takes 30 minutes; a document fetch waits on a human). Without transactions, a crash in the middle of "attach document → create pages → extract requirements → link evidence → build checklist" leaves half-written junk that is very hard to reason about later.

Example: the machine sleeps during a document fetch. With transactions, the tender either has the document fully attached or it does not. Without them, you get a document row whose pages are missing and five requirements citing page numbers that do not exist — which breaks rule 1.

> **Is this a money decision?** No. Postgres is free, Qdrant is free, this is entirely about engineering effort.

**Qdrant — because "find passages that mean the same thing" is not a query a relational database does well.**

Example: a user asks *"construction of road"*. The tender document says *"road construction works"*. Those are the same thing. Postgres text search matches the word `road` but cannot tell you these phrases are equivalent. A vector search can.

**Groq — because Qdrant returns passages, not sentences.**

Retrieval gives you three paragraphs. A model turns them into an answer. The model's job is **phrasing only** — it may reword, summarise and connect, but every fact it states must come from the retrieved passages, and every one must be cited.

## 3.3 The rule that makes this safe

> **Qdrant is disposable. If you delete the entire Qdrant volume, you must lose nothing but re-indexing time.**

This single rule decides many small things later:

- Qdrant stores no text that does not also exist in Postgres.
- Qdrant stores no state — no "which chunks still need embedding" (that lives in Postgres).
- Deleting a document is a Postgres soft-delete plus a *reconciler* that sweeps orphaned vectors, not a fragile cascade.
- Backups are a Postgres job. Qdrant snapshots are a convenience.

If Qdrant ever becomes the only place something exists, that is a bug, not a design choice.

## 3.4 A second split, inside Postgres

Postgres itself splits into two halves, and keeping them separate is what stops the system duplicating public information per customer:

- **Public corpus** — tender, locator, covers, documents, pages, requirements, snapshots. One copy, shared by everyone.
- **Company judgement** — saved/dismissed/bidding flags, evidence matches, assessments, checklists, reminders. Always scoped to a company.

Same principle as the cache: *public facts are shareable; judgement never is.* The tables are listed in §6.3.

---

# Part 4 — System map

```
                          ┌──────────────────────┐
                          │   React frontend     │  (local app, port 5173)
                          │  Tabs: Discover,     │
                          │  Documents, Reqs,    │
                          │  Suitability,        │
                          │  Eligibility,        │
                          │  Checklist, Legal,   │
                          │  Changes, Ask        │
                          └──────────┬───────────┘
                                     │ REST /api
                          ┌──────────▼───────────┐
                          │  FastAPI backend     │  routes → services → repositories
                          └──────────┬───────────┘
             ┌───────────────┬───────┼────────────┬──────────────┐
             ▼               ▼       ▼            ▼              ▼
     ┌────────────┐  ┌───────────┐ ┌──────┐ ┌──────────┐ ┌────────────┐
     │ Scheduler  │  │ Job queue │ │Groq  │ │ Qdrant   │ │ Postgres   │
     │ (24h +     │  │ (durable, │ │(phras│ │ (vectors)│ │ (truth)    │
     │  startup   │  │ in PG)    │ │ ing) │ │          │ │            │
     │  catch-up) │  └───────────┘ └──────┘ └──────────┘ └────────────┘
     └─────┬──────┘        │
           │               │
           ▼               ▼
     ┌──────────────────────────────┐
     │ Browser worker (Playwright)  │──► Chromium (headed, one at a time)
     │ portal walk + doc fetch      │         │
     └──────────────────────────────┘         ▼
                                     Government portals (.gov / .nic)
```

**Two kinds of work, deliberately separated:**

| | **Discovery** | **Document acquisition** |
|---|---|---|
| Who triggers | Scheduler, unattended | User, or auto-flag on a tender they intend to bid |
| Needs a human? | **Usually no** — listing pages are normally readable; a blocked walk parks for the same visible-window handoff | **Yes** — captcha per file, possibly |
| Duration | Minutes | Minutes to tens of minutes |
| Failure impact | "No new tenders today" | "This tender has 3 of 5 documents" |
| Volume | Everything published | Only tenders the user actually cares about |

That table is the single most important operational insight in this document: **the expensive, human-blocked part is small and rare, if you keep it separate from the cheap, unattended part.**

---

# Part 5 — Components, one by one

## 5.1 Authentication and company profile **[BUILT]**

- Signup sends a 6-digit code to email (Gmail SMTP). Codes are hashed, single-use, expiring (10 min), rate-limited (5/hour, 60 s cooldown, 5 attempts).
- **Registration is enumeration-resistant.** `POST /api/auth/register` returns the same `202` and the same sentence for a new, pending or already-registered address. The user and company are created only when the signup code is accepted; until then the pending challenge is an `otp_codes` row keyed by email, carrying a `payload` with the hashed password and company name.
- Verified accounts may optionally sign in with an email code instead of a password. A **code-only** account (no password) can do this; it must set a password before it may disable code sign-in. `PATCH /api/auth/security` confirms the change with the current password or a fresh email code and audits it.
- The JWT carries `sub` and a `company_id` claim, but the server **reloads the user and company from Postgres on every request** and authorises from those rows, never from the claim.
- **No roles.** Every authenticated user in a company may annotate the global corpus within the rules in §6.5. There is no admin capability to erase evidence.
- Every company-owned record is **company-scoped**. A company can never see another company's rows. Cross-tenant access returns 404, never 403. This is not optional - see rule 9 and the tenant discussion in 5.11.

## 5.2 Company evidence library **[BUILT]**

The company uploads its own documents: registrations, ISO certificates, turnover statements, past project completion certificates. Each has a type and an expiry date.

**Why it matters:** eligibility is decided by matching tender requirements to *this* library. An expired certificate is not evidence. This is where rule 4 does its work.

Example: tender requires "ISO 9001 certification valid on the bid due date". Company has an ISO cert that expired last month. The result is **Fail** — a confirmed contradiction, not `Unknown`.

## 5.3 Discovery — the scheduled scrape **[BUILT, with scheduling change PLANNED]**

### What it does

Walks the portal, reads listings, and stores every tender that passes the listing gate. All of this is **deterministic code** — no AI.

### Why it is code and not an "agent"

An AI agent is for **discovering an unknown path**. Once the path is known, code is faster, cheaper, debuggable and repeatable. We now know this portal's path, so we wrote it down.

The walk:

```
directory → choose organisation → read list → filter rows
   → click notice → read it → back to list → next row
   → next page of list → next organisation
```

Every notice is opened **inside one browser session**, because the portal binds a notice page to the session that opened it.

### The listing gate (why some rows are skipped)

A tender is **stored** only when all of these hold:

1. It names **one piece of work** (not a whole category page).
2. A closing date was read **and it is in the future**.
3. The notice is Indian.

And it must **not** be an index/award page, and must not be past its deadline.
These are objective, profile-free rules: the corpus is global.

A tender is **listed for a company** only when the stored work also matches that
company's services, sectors or keywords, with the ambiguity guard below. That
match is computed at **read time**, never used as a storage filter. Two
companies can therefore see different lists over one shared corpus, and adding a
keyword does not require re-scraping.

### Matching: four roles and seven rungs

The company profile is not a flat keyword list. Every declared word and every
confirmed profile item is classified deterministically into one of four roles:

| Role | Example | Meaning |
|---|---|---|
| `core` | `document scanning` | a confirmed capability or a delivered project's work family |
| `adjacent` | `web solutions` declared but not confirmed | specific work, unconfirmed |
| `support` | `technical support`, `document retrieval` | how the work is delivered, not the work |
| `generic` | `technology`, `web pages`, `software` | recall only |

The rungs are ordered so a generic word can never outrank real work:

1. full core phrase, or core plus adjacent/support evidence;
2. core alone or a partial core token;
3. adjacent plus region;
4. adjacent alone;
5. support plus region;
6. support alone or generic-only;
7. no usable match — hidden.

Tiers 1-3 are `strong`; 4-6 are `possible` and stay visible; 7 is `hidden`
unless `include_hidden=true`. A whole phrase counts two units, a single term or
partial token one. The score is only a band inside a rung (capped at 95), not a
second ladder. Plain-text variants (plural forms, `"B of A"` word order) are
deterministic, and every match is explained in `relevance.reasons`.

There is deliberately no corpus-rarity pass in the read path: it never promoted
a rung, and sampling titles paid a corpus scan on every page request. A place,
authority or organisation chain is never work evidence; it only supplies the
region context in rungs 3 and 5.

Example: a profile keyword `AMC` is generic, so a notice titled *"AMC of lift at
hospital"* in a different state is hidden, not listed. A confirmed capability
`document scanning` matched by *"Digitisation of court records"* is a core
semantic match at tier 2.

"Not relevant" feedback demotes the matched terms (`0.15` each, clamped to tiers
4-6). It never bans a tender, and repeats of the same term do not stack.

### Date reading — the trap

A closing date is read from a **labelled** closing/submission word. A date labelled *publish* or *open* is never used as the closing date.

Unlabelled table rows use the latest date in the row — **except** a CPPP-style row (`S.No`, e-Publish, Closing, Opening, title, organisation chain), which takes the **middle** date as the closing, because the last one is the opening date.

> Why this matters: reading the opening date as the closing date tells a user they have two more weeks when the bid actually closed yesterday.

### Portal-specific robustness

- `[Name] [Ref][2026_NHAI_295179_1]` is **split**, not read as prose: first bracketed group = title, the group shaped like a portal id = tender id, the other = reference, first organisation-chain segment = authority. Nothing is invented when a group is missing.
- A notice page is read as **one** tender; its own fee/cover tables are never treated as more tenders.
- A field ends where the next field's label begins, so a one-line summary does not swallow the following field (`NHAI HQ Pincode 110075`).
- A zero or `"NA"` value is **no value**.
- A page the portal answers with an error ("Not Found") is never stored as a tender — otherwise you get a card with no authority, reference or value.
- `/nicgep/` links are rewritten to `/eprocure/`, because the portal 404s the old application.
- Organisations are tiered by word-boundary matching: `broadcast` must not match `road`. A road-specific body outranks a generic public works department whose list mixes roads, hospitals, courts and electrical work. A profile term that is itself only a generic public-works word (`PWD`) does not promote a generic organisation.

### Dedupe — one tender is one record

Identity is the portal tender id (`tender_identity_key`) when one was read. A list row, a fetched notice and a browser read all merge into one record. A second read **never empties a field the first read had** (`_carry_forward`).

When no portal id was read, the fallback is
`sha256(portal_id | normalised_reference | normalised_work_title |
normalised_organisation)` - **no dates**. A manual upload has
`source_type='manual_upload'` and
`sha256('manual' | normalised_work_title | first_document_sha256)`. If a portal
and an aggregator row turn out to be the same notice, the portal row is
canonical; the aggregator row records `merged_into_tender_id`/`merged_at`.
Cross-portal copies are surfaced with `cross_portal_key` and never auto-merged.

### Why you only ever saw 4–8 tenders

Not a bug, not a mystery — configuration:

| Knob | Default | Effect |
|---|---|---|
| `AGENT_WALK_MAX_NOTICES` | 10 | total notices read per run |
| `AGENT_WALK_MAX_NOTICES_PER_ORG` | 4 | cap per organisation |
| `AGENT_WALK_MAX_PAGES` | 5 | list pages per organisation |
| `AGENT_WALK_MAX_ORG_PAGES` | 8 | pages used to find organisations |
| `DISCOVERY_MAX_TENDERS` | 25 | ceiling on tenders listed |

Two organisations × 4 notices = 8, minus rows the gate drops. That is the whole explanation. Note the design intent behind the per-organisation cap: **one department must not be able to hide every later one**.

## 5.4 The three lifetimes (the fix for "session expired")

Three completely different things were being treated as one:

| Thing | Lifetime | Correct treatment |
|---|---|---|
| Browser session | **Minutes** | Disposable. Throw it away without regret. |
| The notice page | **Weeks** | Reachable by tender id, not by stored URL |
| The tender record | **Forever** | Lives in Postgres |

**The original bug:** the user's "fetch documents" action depended on the *shortest* of the three. By the time the list reached the UI and the user clicked, the session was 2–3 minutes from death. With 5 tenders it was mostly survivable; at scale it is fatal.

**The fix: never store a URL, store a locator.**

```
Locator (per tender, in Postgres)
  portal          "cppp"
  tender_id       "2026_NHAI_295179_1"
  reference       "NHAI/HQ/2026/114"
  organisation    "NHAI → Regional Office, Pune"
  work_title      "Road construction, NH-48, 12 km"
  published_date  2026-10-20
```

Why a locator and not a URL: the portal's notice URL contains a **session token**. Save it, and a day later it is a dead link pointing at a session that no longer exists. Save the *identity*, and any future session can navigate back to it.

Example: the list is scraped Monday night. The user clicks "fetch documents" Thursday morning. Thursday's session has never seen that URL. It opens the portal, walks to NHAI's list, finds the row whose tender id matches, clicks it, and proceeds. **Nothing about Monday's session is needed.**

## 5.5 Jobs - durable work with honest states **[BUILT]**

A "job" is background work with a stored state, not a spinner in the UI.

```
queued → running → (needs_you) → running → done
                          │
                          ├→ partial   (some files attached)
                          └→ failed    (with a reason the user can read)
```

**Why durable and not just a thread:** this is a local app. The laptop sleeps, the app restarts, the user closes the window. A job that exists only in memory evaporates. Job rows live in Postgres, so:

- A document fetch waiting on a captcha can be left overnight and picked up tomorrow.
- A scraper that dies mid-run resumes from its last checkpoint.
- "Partial" is a real, visible state: *"3 of 5 documents attached, 2 need your input."*

Rules:

- **One browser at a time.** Scheduled scrape, document fetch and captcha window share a single-lane queue, held in `lane_leases` with an expiry (not an in-memory mutex, not `app_state`).
- **A user-initiated job outranks the scheduler.** The human is standing there; the robot can wait.
- **A `needs_you` job releases its worker thread.** It keeps the browser session under a bounded lease (`AGENT_CHALLENGE_WAIT_SECONDS`, default 600) and marks the lane `reserved_for_challenge`; other browser jobs queue, non-browser jobs continue.
- **Abandoned captcha expires on its own**, otherwise one walked-away prompt blocks the lane forever. Expiry drops the session and records `partial` with "file request expired".
- **A browser session is a thread, not a dictionary entry.** Playwright's sync objects belong to the thread that created them, so the whole walk/fetch body runs on one owner thread and a `needs_you` resume reaches that same thread even when another worker claims the job. Ignore this and greenlet raises "Cannot switch to a different thread" — which used to surface as a per-file error and lose the captcha the human had just answered.
- **A parked session is idle, so the resume pumps it.** The download a human's answer produced is delivered by the page's event loop, and nobody was running it while the job waited. The resume waits briefly for that event (`wait_for_download`) before deciding no file arrived; reading the queue first would re-request the file and ask for a second captcha.

## 5.6 The scheduling rule - 24 hours with startup catch-up **[BUILT]**

A timer inside a local app **only fires while the app is running**. Set "every 24 hours" and close the laptop at 6pm, and nothing runs the next day — silently, with no error.

So the rule is not "run at midnight". It is:

> **On startup, if the last completed run is older than 24 hours, run now. Then continue on the 24h cadence from that run.**

Stored fact: `last_completed_run_at` - **global**, because the corpus is global.
It is not per company: a run that fetched the news once has served every company.
The scheduler is a job producer; the run itself is a `jobs` row of kind
`scrape_portal`, and the canonical run resource is that job.

- Compare against an hour *elapsed*, not against a wall clock: "it's 9am, not midnight" must not block an overdue run.
- Update it on **completion**, not on start, or one failed run buys 24 more hours of nothing.
- Store it in the **database**, not a variable or a file, so it survives restarts (it should also survive a database restore without resurrecting a stale schedule — hence "last completed", not "last attempted").

Example:

| When | Event | Result |
|---|---|---|
| Mon 22:00 | run finishes | `last_completed = Mon 22:00` |
| Tue | laptop off all day | nothing (nothing lost — no user was waiting) |
| Wed 09:00 | app opens | 35 h elapsed → **run now**, unattended |
| Wed 09:40 | run finishes | `last_completed = Wed 09:40`; next due 09:40 Thursday |

Also: make the run **idempotent**, so a manual "refresh now" is harmless and is literally the same code path.

**What 24h costs you:** staleness, and only for tenders closing *today*. Mitigate by showing the last-run timestamp on the list rather than by scraping more often. Revisit cadence when there is real usage.

## 5.7 Document acquisition — the human-gated part

### The flow

```
user clicks "fetch documents"
  → job queued (Postgres)
  → browser lane free? if not, wait
  → fresh browser session
  → navigate: portal → organisation → list → row matching tender id → click notice
  → read document links (PDF, XLS, ZIP…)
  → for each link, in order pdf → file → unknown → zip:
        request it
        ├─ file arrives              → attach
        ├─ captcha page arrives      → open the link as a page and verify the form
        │     → pause job as "needs_you"; show the captcha picture and the page
        │     → human types the answer in the app
        │     → software fills the portal's form once and drops the answer
        │     → file arrives as the answer → attach
        └─ error / not a PDF         → report, do not attach
  → no individual file arrived? open the zip bundle, attach each PDF inside
  → job → done or partial
  → trigger requirement extraction automatically
```

### The captcha, precisely

This is the part everyone gets wrong, so it is spelled out.

**A captcha page is not a separate window. It *is* the server's response to the file request.**

You ask for `doc.pdf`. The server does not send `doc.pdf`. It sends an HTML page containing a picture and a form. When the human answers and the form is submitted, **the bytes returned by that submission are the PDF**. There is no second step.

The portal's form carries hidden fields that state *which file you were asking for*: `formids`, `seedids`, `session`, `tokenSecret`, `If_17`, plus `component=frmCaptcha`, `page=DocDownCaptcha`, `service=direct`, action `/eprocure/app`. The answer goes in `captchaText`. The picture is `img#captchaImage` as a `data:image/png;base64,...` source.

Consequences that must be respected:

1. **The response bytes are the file.** Do not "solve, then re-download with a plain HTTP client". The file was handed to you in that submission's response; nothing is left sitting at the original link.
2. **Single-use and time-limited.** Often 60–120 seconds.
3. **Do not re-open the page after answering to "get a fresh one".** That issues a new token and **loses the pending file request**. Read the page *before* the answer (that is the request the form belongs to), answer that form once, or restart that document.
4. **Possibly per file, not per session.** Some portals gate once and then let the session through; CPPP-style document downloads often gate **per request**. Five PDFs can mean five human answers. This is the true source of the "30 minutes for 5 tenders" experience, and no tool removes it.
5. **A page that merely *mentions* a captcha is not a captcha.** The portal's markup mentions it everywhere. Recognition requires **both** the answer input and the picture.
6. **The fetched HTML is not always the page.** The portal can draw the captcha on the navigated page while the `fetch()` response is a wrapper, so a page response is opened the way a person opens it and read there. Judging it from the bytes alone is what produced "the portal answered with a page, not a document".
7. **An accepted answer's response is read, never re-requested.** The submission returns the file itself or a page that carries its own download link. The original document URL is never requested again after an accepted answer, because that request is what draws a new captcha; a page that hands over no file marks that link done with a clear error. One fetch shows at most six pictures in total, then reports the rest instead of asking forever.

### Why the human answers in the app

Two possible designs:

| | Visible browser, human types in the portal | **In-app relay (chosen)** |
|---|---|---|
| Human sees | The portal's own page, zoomable | The captcha picture, with a full page screenshot for context |
| Who types | The human, in the portal | The human, in the app |
| Who submits | The human, natively | The software fills the portal's own form, once |
| Fragility | The browser does it correctly | The browser still does it; the code only sets one field and clicks the portal's Submit |
| Session certainty | Highest | Highest — the answer is submitted in the same browser session that requested the file |
| Requirement | Headed browser, window raised | Headed browser, window stays minimized |

The relay is chosen because the run can keep its window minimized and covered (a person does not have to hunt for a window mid-run), the two pictures tell the user exactly what they are answering, and the submit still happens in the portal's own page. It changes no domain rule: a **human** reads the characters and supplies them; the software never invents, guesses or solves a challenge, and it keeps no copy of the answer.

Non-negotiables for this design:

- **Never ask for portal credentials.** The human may answer a captcha in the app; a login wall is a *stop*, not a request for credentials.
- **One captcha window at a time**, in a single queue.
- **Park, do not fail**, if the human walks away. Resume later - cheap, because the locator can find the notice again.
- **Release the worker, keep the session.** A `needs_you` job returns its worker thread to the pool but holds the browser session under a bounded lease (`AGENT_CHALLENGE_WAIT_SECONDS`, default 600). The lane row is marked `reserved_for_challenge`, so other browser jobs queue while non-browser jobs continue.
- **The software never solves a challenge; a human supplies the answer.** `POST /api/jobs/{id}/challenge/answer` carries that answer into the portal's own form once and drops it: there is no answer column, log line, metric or echo, and `POST /api/jobs/{id}/challenge` remains the signal-only path. A rejected answer keeps the same file request pending and shows the portal's *new* picture (bounded retries per link); a portal that never accepts one is reported instead of looped on. The same relay resumes a parked walk: the walk keeps its browser session, restarts from the directory after the answer, and reports a second challenge instead of parking again. An unattended headless run reports the block instead of parking because nobody is there to answer it.
- **A closed window is a partial, not a retry.** If the human closes the portal window, the session is gone and the page cannot come back. The job records `partial` with one clear note ("the window was closed… start it again"), closes the challenge and does not retry automatically; a driver that swallowed the closed-target error still marks itself lost, so the note never turns into a per-file error. An answered challenge restarts the attempt budget, because a handoff is progress, not a failure.
- **The window works minimized and covered.** While the software reads the portal the window sits in the taskbar (CDP `windowState: minimized`; a browser with no window falls back to a small 480×360 window) and its content is blurred by the page itself (a CSS `backdrop-filter` cover with a moving sheen, re-applied on every navigation). `reveal` restores it only for a human-driven walk; a fetch challenge never calls it, because the captcha is answered in the app and the screenshot is taken while the window is minimized (verified not blank). The cover is drawn by pseudo-elements, not by a DOM node: it cannot appear in the page text the extractor reads, and clicks pass through it. A minimized window still takes clicks, typing and navigation, so the walk keeps working while it is in the taskbar. The challenge panel shows the captcha picture, the full page screenshot, the answer box and a note that the answer is carried into the portal once and never saved; a rejected answer refreshes the picture.
- **Expiry is honest.** If the lease runs out, the session is dropped and the job records `partial` with "file request expired"; it does not pretend the file arrived.
- **Never auto-submit, never loop** on a second captcha: park again with the portal's new picture while the retry budget lasts, then report it.
- **Cancel stops the work, not just the UI.** `POST /api/jobs/{id}/cancel` (and the discovery run cancel) sets the job cancelled, expires its open challenge and closes its browser session. A running walk or fetch sees the cancellation at its next heartbeat (`JobCancelled`) and stops; a parked job is closed at once.

### The cover table — the portal's own document list

The notice page carries a table of covers: `cover no` (a serial), document type, description. That table is a **ready-made expected-document list**, and it turns "partial" from an unknown gap into a named one.

What it is used for:

- **Naming what is missing.** `files < covers` → incomplete, and the UI says *"cover 3 (BOQ) not yet fetched"* rather than *"4 of 5"*. That is the difference between a status and an instruction.
- **A tender-specific checklist source.** Cover 3 = BOQ → checklist item "prepare and upload BOQ". This satisfies rule 7 without inventing anything.
- **Cheap change detection.** If `v1` lists 5 covers and `v2` lists 6, that is a material change you can surface **without opening a single PDF**. Deterministic, no AI, no OCR.
- **Completeness sanity.** `files ≥ covers` → complete, even when extra files (fee receipt, corrigendum attachment) map to no cover.

What it must **not** be used for:

- **It is not an arbiter of "done".** The portal's claim is not ground truth. A cover may have no downloadable file at all; five covers may arrive as one `bundle.zip`. So never fail a fetch because a cover has no file — report it: *"cover 4 (Integrity Pact) has no downloadable document"*, and mark the tender complete-with-a-note.
- **It is not stable across versions.** A corrigendum can renumber covers. Store `cover_no` **and** the type/description, and match across versions on the description, not the number.

### Partial state is the tender's, not the company's

Because the corpus is global (§6.1), document presence is global too. So the state is:

> *"This tender has 4 of 5 covers; the last attempt failed on cover 3."*

…not *"Company A has 4 of 5."* If it were per-company, a second company would re-pay the captcha for work already done.

The consequence worth stating explicitly: **captcha effort is paid once per tender, not once per company.** If Company A answers five prompts for tender X, Company B gets all five instantly. That is the single biggest practical relief on the human bottleneck — 500 prompts for 100 tenders is a cost paid by the whole user base, not each company.

What stays per-company is **intent**: who asked first, who is watching it, who gets notified.

### Throughput reality

Document acquisition is **human-bounded**: realistically 2–5 answers per minute with an attentive person. Therefore:

- Fetch documents **only for tenders the user intends to bid on.**
- Treat **partial** ("3 of 5") as a normal outcome, not an error.
- Always try the un-gated document host first: every file obtained without a prompt is throughput kept.
- Use the cover table to spend prompts on files that matter, not on the last annexure.

## 5.8 Field extraction and the citation gate **[BUILT]**

Documents are parsed, chunked, and sent to an AI model (Groq, `openai/gpt-oss-120b` by default) to propose requirements. **The proposal is untrusted.** Each item is validated on its own, so one bad item cannot sink a batch.

The gate, in order:

1. Is every field present and of the right type?
2. Normalise the quoted excerpt.
3. Search for it **on the cited page of the cited document**.
4. Decide the state:

| Outcome | Stored as |
|---|---|
| Excerpt found on the cited page, in the text layer | `verified` |
| Excerpt found **only** in OCR text (scanned page) | `needs_review` |
| Not found | `unverified`, with a reason |

- `needs_review`, `rejected` and `accepted_ocr` are **human states**, and every change is audited with the actor.
- Re-running extraction skips duplicates. `replace=true` sets AI-origin requirements for that document `is_active=false` first - it never hard-deletes, and never touches an `origin='human'` row. Dependants keep their foreign key to a row that still exists.
- OCR exists as a bounded fallback (English Tesseract, 40 pages max, 45 s timeout) and its output is always **labelled as OCR** - that is why OCR-only citations start as `needs_review`, and become `accepted_ocr` only when a human accepts them. They never become `verified`.
- Every requirement also carries `title`, `obligation` (model interpretation, never a citation), `deadline_text`/`deadline_at`, `consequence_text`, `acceptance_criteria`, `issuer`, `signatory`, `evidence_check`, `form_ref`, `model` and `prompt_version`. `mandatory_state` is `mandatory` / `not_mandatory` / `unknown`; it replaces the old boolean, because "we did not read a mandatory word" is not "not mandatory".
- A navigation hit (contents/index row) is stored with `is_navigation=true`, excluded from assessment and hidden by default. Excerpt uniqueness is per page: `(document_id, page_index, excerpt_hash) WHERE is_active`.

## 5.9 Assessment — score versus gate **[BUILT]**

> **Rebuilt in `backend_v2` as Stage E.** The old `backend/` implementation
> remains the behavioural oracle while parity is proven.

Two separate outputs, never merged:

**Soft fit score** (a number, for ranking):

| Component | Weight | Formula |
|---|---|---|
| Profile fit | 25% | `25 × matched profile terms / total profile terms` |
| Citation verification | 20% | `20 × (verified + accepted_ocr) / active requirements` |
| Confirmed mandatory evidence coverage | 40% | `40 × mandatory criteria with confirmed non-expired evidence / mandatory criteria` |
| Tender document readiness | 15% | `15 × documents present / covers expected` (0 when no covers are known) |

**Mandatory eligibility** (a verdict, for the decision):

| Verdict | Requires |
|---|---|
| `Pass` | a **verified** citation **or** a human-confirmed `accepted_ocr` citation, **and** human-confirmed, non-expired evidence |
| `Fail` | a confirmed contradiction |
| `Unknown` | everything else - including "no evidence found" |

The gate is built from `assessment_criteria`, one active row per mandatory
requirement. Aggregate: any `Fail` -> `Fail`; else any `Unknown` -> `Unknown`;
else `Pass`. `accepted_ocr` may only back a `Pass` when the match was
human-confirmed and the OCR provenance is recorded; the UI says "Pass - OCR
citation, human-confirmed", never "verified".

Rule 3 in action: score 92/100 with an unproven turnover requirement is **`Unknown`**, not a pass, and the UI shows the gate before the score.

## 5.10 Checklist and legal obligations **[BUILT]**

- Checklist generation selects mandatory / submission / compliance / form-referenced requirements. It is **idempotent** (`UNIQUE (company_id, tender_id, requirement_id, phase)`) and preserves human status and owner, so regenerating never wipes someone's progress.
- Generation copies the citation onto the task (`document_id`, `page_index`, `clause_ref`, `excerpt`, `source_url`). Re-extraction therefore cannot rewrite what a task was based on.
- Post-award obligations (performance guarantees, contract conditions) are **separate** from pre-award tasks. Old tasks without a `phase` are treated as `pre_award`.
- Every item states consequence **only if the source states it**; a missing deadline or consequence says "not stated in source", never a guess. Multiple tasks per requirement wait for a future `task_key` - v1 has one per requirement per phase.

## 5.11 Asking questions about a document — **PARKED**

> **Status: parked. Do not build this yet.** The design is retained below so the work is not lost, but it is **not** in the near-term build. Consequence: **chunks, embeddings and Qdrant are deferred with it**, since they exist only to answer questions. Page text is still stored, because citations need it — that is a different thing from vectors.

### Why parked

The feature has too many open conditions to build safely in one pass: which questions are allowed, what "shared" means across companies, how a wrong answer is retracted, and how it interacts with document versions. Each is small; together they are a design of their own. Building the core product first is the right order — and the core is genuinely useful without it.

### Scope, when it is resumed

**Tender-document questions only.** No company-document questions, no "do we qualify", no legal advice. The moment a question mixes a tender document with company facts it becomes an *applicability* answer, which must be **refused** with a pointer to the Eligibility tab. Keeping that boundary hard is what makes the guarantees simple.

### The lean model we agreed on

Answers do not need the heavy `verified` / `rejected` review states that requirements need. That asymmetry is deliberate:

> **Rigour scales with blast radius.** A requirement feeds a score and a checklist — a wrong one changes a pass/fail verdict. An answer only helps someone read a document.

So the answer path keeps three things and no more:

| Check | When | Why |
|---|---|---|
| **Citation liveness** | before serving, always, machine | Is the quoted text still on the cited page of the cited version? Catches replaced documents, moved pagination, changed OCR layers. Thumbs cannot catch staleness, because a stale answer reads perfectly plausibly. |
| **No uncited sentence** | before storing, machine | Reject and regenerate if the model added a claim the passages do not support. This is the only thing stopping a confident summary from drifting off the page. |
| **👍 / 👎** | after serving, human | A 👎 invalidates the cached answer and files a review item. A 👍 only accumulates signal. |

Two rules that go with it:

- **Silence is not approval across companies.** Inside one company, no complaint is a fine reason to keep a cached answer. Across companies it is not: silence means *nobody read it*, and one silently-wrong answer promoted to the shared cache is served to everyone. **Positive feedback may be sparse; negative feedback must propagate to every copy.**
- **The dangerous failure is not a wrong passage — it is the right passage answering the wrong question.** Retrieval can return clause 4.2 verbatim, correctly cited, for a *different package's* EMD. Every citation rule is satisfied and the answer still misleads. Only a human catches that class, so the thumb is the verification step — late and unrecorded, and that is acceptable for answers as long as we do not pretend it is unnecessary.

### Reference design (retained — do not build yet)

The rest of this section is the fuller design worked out earlier. It is kept for when the feature resumes; it is **not** a build instruction.

#### Two loops

```
INGESTION (when a document arrives)
  PDF → page text → chunks → embeddings → Postgres (text) + Qdrant (vectors), same id

QUESTION (when a human asks)
  question → exact-hash lookup → miss → embed → Qdrant nearest chunks
           → fetch those chunks' text from Postgres
           → Groq composes an answer, citing only those chunks
           → Postgres stores answer + citations + versions
```

Note the asymmetry: **the knowledge comes from documents, not from questions.** A question adds a cache entry (its embedding), never new knowledge.

#### Source restriction

Only `.gov` and `.nic` sources are used for answering, and only with citations. This is rule 9 and it exists because a confident wrong answer about a legal deadline is the most expensive failure this product can have.

#### Chunk design

- Chunks are **section-aware**: a law is chunked at section granularity so a retrieved chunk carries its section number and can be quoted with a citation. A PDF is chunked by page and paragraph.
- Chunk row in Postgres (the truth):

```
chunk_id        uuid (deterministic — see Part 7)
document_id     fk
document_version  v1 / v2 / …
page            int
printed_label   text  (if it differs from the page index)
section         text  (e.g. "Clause 4.2", "Section 12(3)")
text            verbatim
content_hash    sha256
model_version   e.g. "bge-small-en-v1.5"
embed_state     pending | done | failed
embedded_at     timestamp
superseded      bool
```

- Qdrant point: `id`, vector, and a **thin** payload — `document_id`, `tender_id`, `page`, `version`, `content_hash`. No text (it lives in Postgres).

#### Caching: evidence is shareable, conclusions are not

Three tiers, safest first:

| Tier | Key | Reusable across companies? | Why |
|---|---|---|---|
| **1. Retrieval** | normalised question + document version set | **Yes** | Depends only on the question and public documents |
| **2. Answer, same company** | company + doc version set + question hash + prompt version + model version | No (by construction) | Contains company assessment |
| **3. Answer, cross-company** | only for **document-fact** questions | Yes | The answer is about the PDF, not the company |

The tier-3 classifier is the whole trick:

| Question type | Example | Shareable? |
|---|---|---|
| **Document fact** | "What is the EMD for this tender?" | Yes — cacheable verbatim |
| **Applicability** | "Am I eligible?" / "What do I still need?" | **Never** |

**Why this matters more than it sounds.** Restricting to `.gov` fixes *fabricated citations*. It does **not** fix tenant bleed:

> "Does my company qualify?" → "No — you have no ISO 9001 and your turnover evidence is two years old."

Every cited document is a public government PDF, and the answer still contains one company's private assessment. Serve that to a second company and you have leaked and lied in one move. Hence: **cache the evidence across tenants; cache the conclusion only within one.**

#### The guard on every cached answer

**Never serve a cached answer without re-validating its citations** — for each quoted excerpt, confirm it still exists on the cited page of the cited document version. This is a cheap string comparison, the same discipline already applied to requirements.

If validation fails → discard the entry, regenerate.

#### Version-pinned invalidation

Every cache entry is pinned to the document versions it was written from. The moment `v2` is ingested, every answer pinned to `v1` **for that document** is invalidated and regenerates on next ask. One mechanism does two jobs: document versioning and cache invalidation.

Never validate a `v1` answer against `v2` text — that is not staleness detection, that is a false alarm.

## 5.12 Company understanding — what the company says it does **[BUILT]**

A profile of work terms answers "which words recur in this company's document".
It does not answer the question discovery actually has to answer: *is this the
same family of work we do?* Two firms can describe the same work with no shared
keyword, and one firm's client list can read like a different industry.

So the company's own documents are read a second time, for two kinds of cited
statement:

| Kind | What it states | Used for |
|---|---|---|
| **Capability** | a kind of work the company says it performs | the work-family comparison, and mapping a requirement to "have we done this before" |
| **Experience** | a named project or product it delivered, with client/value when stated | stronger evidence than a capability in requirement mapping |

Both carry `page_index` (1-based, the same numbering the corpus uses),
`printed_label`, the verbatim `excerpt`, `is_ocr`, and a `state`
(`proposed` / `confirmed` / `rejected`). The reader validates every quote
against that page before storing it, so an invented item is dropped rather than
shown. A company-name line is never a capability.

**Only confirmed rows are used for a capability claim.** A proposed item is
shown on the Documents page with its citation and waits; a rejected item stays
stored so the human can change their mind, and is never used. This is the same
rule as evidence: the model may propose, the person decides. The company brief
(5.13) may also carry a declared **adjacent** term that is not confirmed from a
document; it can be compared, but it ranks below a core capability and is never
presented as one.

### The closed-label judge

Every tender with a stored rules verdict is sent to the model, **without its
tier, score or bucket**, together with the four brief role groups and the exact
company lines. The model may answer only `yes`, `no` or `not_sure` — is this the
same family of work? No score, no reasoning essay, no eligibility call, no new
capability, no invented requirement.

Each answer carries the notice `title`, an exact `tender_evidence` line, an
exact `company_evidence` line, a `match_type` and one short reason. Both quotes
are checked against the stored lines; a missing side is a valid `no_evidence`
answer. The model cannot invent a quote, and it is never asked to rank the
tender.

Three guards make the answer safe to show:

- **The quote rule.** A `yes` counts only when both quoted lines are found in
  the stored tender and company evidence. An unanchored or paraphrased `yes` is
  stored `unsure` with `needs_confirmation` and displayed as "AI suggested —
  confirm". The model's word alone is never the evidence.
- **The role rule.** The ranking effect is deterministic and comes from the
  matched company line's role, not from model confidence: `exact_core` -> tier
  1, `semantic_core` -> 2, `adjacent` -> 3 with the region otherwise 4,
  `support_only` -> 5 with the region otherwise 6, `generic_only` -> 6.
- **A `no` only demotes.** It sets tier 6 at the bottom of the possible band and
  appends a reason; it never hides, deletes or dismisses the tender. The
  adjustment is applied on the **read path** on top of the cached rules verdict,
  so the verdict cache stays rules-only and the comparison cannot alter an
  eligibility answer.

Judgments are cached per company/tender by an input hash covering the tender's
content hash, the confirmed capability signature, the brief payload signature
and the rules version. A repeat read does not re-ask; a brief edit, a capability
edit or a re-published notice does. The stored row keeps the model, prompt
version and both quoted lines, so an answer can be audited back to the inputs
that produced it.

### Requirement coverage

"Have we done this before?" is a separate, user-triggered comparison. Each
tender requirement is placed in one bucket:

| Bucket | Meaning | What the user sees |
|---|---|---|
| `covered` | a confirmed capability/experience shows the same work | the company page and verbatim line |
| `partial` | related work, but not the same scope | the citation plus what is missing |
| `new` | nothing confirmed shows this work | "new work" — a prompt to look, not a failure |
| `unknown` | the reader could not place it (not work-bearing, or skipped) | no claim either way |

`unknown` is not `new`: silence is not evidence that the company has never done
the work. A requirement the reader skips is stored `unknown`, never `new`. Both
sides are cited — the tender excerpt that was compared and the company excerpt
it was compared with — and the company-side citation is copied from the stored
row, never from model output. Unchanged requirements reuse their stored mapping
through an input hash. One bounded call per run; the user starts it.

A checklist task generated for a `new` requirement carries that in its evidence
check, so the task says what to establish. Coverage never marks a mandatory
criterion Pass or Fail: the deterministic gate remains the only thing that does.

Migration `0008_company_understanding` adds `company_capabilities`,
`company_experiences`, `tender_similarity_judgments` and
`requirement_experience_matches`, the `understanding_*` columns on
`company_documents`, and the `judge_tender_similarity` job kind.

## 5.13 The company brief — editable, versioned, cited **[BUILT]**

The PDF reader answers *what does the company's document say*. The brief answers
*what should discovery treat as this company's work*, in the company's own
words. It is the only company-side text the AI judge is given, and it is
deliberately separate from eligibility.

| Group | Source | Can rank? |
|---|---|---|
| `core` | a confirmed capability or a project's work family | yes, strongest |
| `adjacent` | a specific service/sector declared in the profile, not confirmed from a document | yes, below core |
| `support` | delivery words (`technical support`, `document retrieval`, `version control`) | never as core |
| `generic` | recall words (`technology`, `web pages`, `software`) | recall only |

Projects are the strongest fit evidence. Certifications are kept for
eligibility evidence and never make a tender the company's kind of work. A term
keeps its page, printed label and verbatim line when it came from a document, so
the two-sided evidence panel can quote it exactly.

A human edit is a **new version**, never an in-place change: a past judgment
still names the words it used. The current version is the only one fit reads.
The service deterministically downgrades a support/generic phrase typed into a
stronger section, so a person cannot accidentally promote `technology` to core
work. A generated brief is refreshed after a capability/experience confirmation
or after an `about_company` document is deleted; a human-edited brief is never
overwritten. The Documents page has an editor for the summary and the four
groups, a version history with "make current", and read-only project and
certification lists with their citations.

Migration `0010_company_brief` adds `company_briefs` and the evidence columns on
`tender_similarity_judgments` (`judged_title`, `tender_evidence`,
`company_evidence`, `match_type`, `evidence_rank`).

### Measuring ranking quality

`scripts/benchmark_relevance.py` runs the deterministic rules layer over
`tests/fixtures/relevance_benchmark.json` and reports precision@10, precision@20,
nDCG@20, top-20 mean grade and strong share, false positives that were listed,
and relevant rows the gate hid. The fixture is synthetic and clearly labelled;
it is never seeded into a database. It exists so a later tweak cannot quietly
trade precision for recall (or the reverse) without the numbers moving. The
AI adjustment is deliberately outside the benchmark: it needs a model key and
may only re-rank evidence the rules already surfaced.

---

# Part 6 — Data model

## 6.1 The split: public corpus vs company judgement

Two different kinds of fact live in this system, and they must not share a table.

| **Global corpus** (public facts about the world) | **Per-company judgement** (private) |
|---|---|
| tender, locator, covers, documents, pages, requirements, snapshots, changes, discovery candidates | saved / dismissed / bidding, evidence matches, assessment criteria, fit assessments, checklist, reminders, audit |

**Listing is a read-time view.** Storage never filters by a company profile, so
the same notice serves every company; relevance decides only what that company
sees. `last_completed_run_at` is global for the same reason: one crawl serves
all tenants.

**Why.** Two companies scraping the same portal would otherwise produce two copies of the same public tender, two document sets, and (later) two embedding sets. That is the redundancy that actually costs money and time. With a global corpus, a document is **fetched once, parsed once, and cited many times**.

The consequence is deliberate: if Company A fetches tender X's five PDFs, Company B sees them immediately. Public documents, shared — fine. What must never leak is *who fetched them* and *what they concluded*. This is the same principle already settled for the answer cache: **public evidence is shareable; per-company judgement never is.**

The split is visible in every table: identity and content are global; flags, scores and verdicts carry `company_id`.

## 6.2 Why the reference number must never be unique

The obvious dedupe key — the tender reference number — is the one field that will betray you. Five ways:

1. **It is often missing.** Many notices have no reference, or `NA`, or a dash. A unique constraint on a nullable column either permits unlimited NULLs (deduping nothing) or forces a placeholder that then collides.
2. **Numbering restarts per division.** `01/2026` exists in NHAI Pune, NHAI Nagpur and PWD simultaneously.
3. **Punctuation drifts.** `NHAI/HQ/2026/114`, `NHAI-HQ-2026-114` and `NHAI / HQ / 2026 / 114` are one tender written three ways.
4. **Corrigenda reuse it.** An amendment carries the *same* reference with a new closing date and is a genuinely new notice (§6.7).
5. **Re-tendering reuses it.** A failed tender is re-floated with the same reference and new dates.

**What is unique: the portal's own tender id** (`2026_NHAI_295179_1`). So:

- **Unique:** `(portal_id, portal_tender_id)` — partial, where the id is not null.
- **Unique:** a computed `dedupe_key` for when no portal id was read (listing rows, aggregators).
- **Indexed, not unique:** the normalised reference, for matching only.

```
dedupe_key = sha256( portal_id | normalised_reference | normalised_work_title | normalised_organisation )
```

**Dates are deliberately absent.** The closing date is mutable - a corrigendum
moves it - so including it would create a second row for the same notice. The
date belongs in snapshots and changes, not in identity.

The fallback only applies when `portal_tender_id` is absent, so a real portal id always wins.

**And dedupe is an upsert, not a skip.** Tomorrow's scrape finds the same tender -> you **merge** it, fill gaps, and record a snapshot **only if the content hash changed**. Skipping the row entirely would mean never noticing that the closing date moved from the 5th to the 12th. "Don't save redundant tenders" means *one row*, not *ignore the notice*.

**Two keys, one service.** Portal id present: upsert on `(portal_id,
portal_tender_id)`. No portal id: upsert on `dedupe_key`. If both match
different rows, the portal row is canonical and the aggregator row gets
`merged_into_tender_id`/`merged_at` and is hidden from listings. Cross-portal
copies are never auto-merged: a non-unique `cross_portal_key` surfaces "also on
the state portal" and leaves the judgement to a human.

## 6.3 Tables and keys

### Identity and tenancy

| Table | PK | Unique | FK |
|---|---|---|---|
| `companies` | `id` | — (company names are not unique in reality) | — |
| `users` | `id` | `email` (citext) | `company_id` → companies |
| `company_documents` | `id` | — | `company_id` → companies |
| `company_document_versions` | `id` | `(company_document_id, sha256)` | → company_documents |
| `company_capabilities` | `id` | `(company_document_id, normalized)` | `company_id`, `company_document_id` |
| `company_experiences` | `id` | `(company_document_id, normalized)` | `company_id`, `company_document_id` |

Indexes: `(company_id, document_type)`, `(company_id, expires_at)` — the expiry index is what makes `Fail` possible without scanning.

### Global corpus

| Table | PK | Unique | FK |
|---|---|---|---|
| `portals` | `id` | `code` (`cppp`, …) | — |
| `tenders` | `id` | **`(portal_id, portal_tender_id)`** partial; **`dedupe_key`** | `portal_id` nullable; `supersedes_tender_id`, `merged_into_tender_id` → tenders (self) |
| `tender_locators` | `id` | `tender_id` (1:1) | `tender_id` |
| `tender_covers` | `id` | `(tender_id, version, cover_no)` | `tender_id` |
| `tender_documents` | `id` | **`(tender_id, sha256)`**; **`(tender_id, document_key) WHERE is_current AND deleted_at IS NULL`** | `tender_id`; `supersedes_document_id` → self |
| `document_fetch_state` | `id` | `tender_id` (1:1) | `tender_id` |
| `document_pages` | `id` | `(document_id, page_index)` | `document_id` |
| `requirements` | `id` | **`(document_id, page_index, excerpt_hash) WHERE is_active`** | `document_id` |
| `tender_snapshots` | `id` | `(tender_id, captured_at)` | `tender_id` |
| `changes` | `id` | `(tender_id, from_snapshot_id, to_snapshot_id, field_key)` `NULLS NOT DISTINCT` | 2× → tender_snapshots |

Indexes: `tenders(closing_at)`, `tenders(portal_id, published_at)`, `tenders(cross_portal_key)`, `tender_documents(tender_id, version)`, `requirements(document_id, state)`.

`tender_documents` carries `version` (`v1`, `v2`, …), `document_key`, `is_current`, `extraction_state`, `extraction_error` and `supersedes_document_id`; see §6.5 for the four levels and Part 7 for the pipeline. `document_fetch_state` holds `last_requested_at`, `last_attempt_at`, `attempts`, `missing_covers`, `last_error` - it is **global**, so a second company never re-pays a captcha (§5.7).

`tenders` also carries `cross_portal_key` (non-unique), `merged_into_tender_id`/`merged_at`, and an `unverified text[]` list of agent/model claims the page did not state. Manual tenders have `source_type='manual_upload'`, a nullable portal, and `dedupe_key=sha256('manual' | normalised_work_title | first_document_sha256)`.

`tender_covers` stores the portal's own cover table **per version**, because it is diffed: a sixth cover appearing is a material change detected without opening a PDF.

### Per-company

| Table | PK | Unique | FK |
|---|---|---|---|
| `company_tenders` | `id` | **`(company_id, tender_id)`** | both |
| `evidence_matches` | `id` | **`(company_id, requirement_id)`** | requirement; **`company_document_version_id` -> versions, `RESTRICT`** |
| `tender_similarity_judgments` | `id` | **`(company_id, tender_id)`** | both |
| `requirement_experience_matches` | `id` | **`(company_id, requirement_id)`** | both |
| `company_requirement_flags` | `id` | **(company_id, requirement_id)** | both |
| `fit_assessments` | `id` | **`(company_id, tender_id)`** | both |
| `assessment_criteria` | `id` | **`(company_id, tender_id, requirement_id) WHERE is_active`** | requirement; `evidence_match_id` nullable |
| `checklist_tasks` | `id` | **`(company_id, tender_id, requirement_id, phase)`** | `(company_id, tender_id)`, `requirement_id` nullable |
| `reminders` | `id` | **`(company_id, key)`** | `company_id`, `tender_id` nullable |
| `audit_logs` | `id` | — | `company_id`, `actor_user_id` |

`company_tenders` uses `saved_at` / `dismissed_at` / `bidding_at` - timestamps, not booleans, so you keep *when* and can filter without extra tables.

`checklist_tasks` carries `phase` (`pre_award` / `post_award`) rather than a separate post-award table: post-award obligations are cited requirements with a different phase, and splitting them would duplicate every citation column. It copies `document_id`, `page_index`, `clause_ref`, `excerpt` and `source_url` at generation.

`evidence_matches` references the exact `company_document_version_id`, never the parent document. Expiry or soft delete recomputes the match to `expired`/`missing` and clears `confirmed_by`; new bytes under an old confirmation would otherwise inherit it silently.

`assessment_criteria` is the per-criterion gate. Aggregate: any `Fail` -> `Fail`; else any `Unknown` -> `Unknown`; else `Pass`.

### Work and system

| Table | PK | Unique | FK |
|---|---|---|---|
| `jobs` | `id` | `idempotency_key` (nullable - safe double-enqueue) | `company_id` nullable |
| `runs` | `id` | — | `company_id`, `job_id` |
| `challenges` | `id` | — | `job_id` / `run_id` |
| `lane_leases` | `name` text | **`name`** | — |
| `discovery_candidates` | `id` | — | `run_id` |
| `app_state` | `key` text | **`key`** | — |
| `otp_codes` | `id` | — | `user_id` nullable |

`app_state` holds `last_completed_run_at` and (later) `active_embedding_collection`. One key/value table beats three single-row tables.

`lane_leases` holds the browser lane: `owner_kind`, `owner_id`, `acquired_at`, `lease_expires_at`, `reserved_for_challenge`, `expires_at`. `app_state` cannot be the lock because it has no expiry. A `needs_you` job releases its worker thread but keeps the session lease; other browser jobs queue, non-browser jobs continue.

`discovery_candidates` records every URL the crawler saw: `kind` (`source`/`tender_candidate`), `outcome` (`stored`/`rejected`/`unreadable`/`unlinked`), `reason`, `listing_url`, `first_seen_at`, `last_seen_at`. Rejections are rows, not counts, so "read and discarded" is provable.

`jobs` indexes: `(state, run_after)`, `(lane, state)`, `(company_id, created_at)`. `runs` carries the counters (§9's last row depends on them). `challenges` has **no answer column**; the human types into the portal page, and `POST /challenge` carries no answer.

`otp_codes` carries `payload jsonb` for a pending signup (hashed password + company name) and `user_id` may be null. Cleanup deletes expired rows (replacing the Mongo TTL index).

### Deferred with §5.11 (design retained, not built)

`chunks`, `answers`, `retrieval_cache`. Their shapes are specified in §5.11 and §6.8; they are **not** part of the near-term build.

## 6.4 Delete behaviour

Decide explicitly rather than inheriting defaults:

| Relationship | On delete | Why |
|---|---|---|
| tender → documents, pages, covers, requirements, snapshots, locator, fetch state | `CASCADE` | the tender is gone; so is its content |
| document → pages, requirements | `CASCADE` | content belongs to the file |
| requirement -> evidence_matches, assessment_criteria, checklist_tasks | `RESTRICT` | a human decision must not vanish silently |
| company_document_version -> evidence_matches | `RESTRICT` + soft delete | a confirmed match must keep naming the bytes it saw |
| company -> anything | `RESTRICT`; soft-delete the company instead | never destroy judgement data by accident |
| anything referenced by a vector index (later) | soft-delete + reconciler sweep, never a bare cascade | two systems, one authority |

Soft-delete columns (`deleted_at`) on `tenders`, `tender_documents`, `company_documents` and `company_tenders` keep the door open for the reconciler and for audit. Requirements are **never hard-deleted**: `is_active=false` retires an AI-origin row, and `replace=true` does exactly that.

## 6.5 Shared requirements — the discipline

Requirements are **global** (they are facts about a public document), which is fine and desirable — one extraction serves everybody. But shared state needs two rules.

**Rule 1 — `verified` can only be set by the citation gate.** Never by a human click. Humans set `needs_review`, `rejected` (the extraction is wrong) and — for OCR — `accepted_ocr`.

**Never collapse `verified` and `accepted_ocr`.** A verbatim text-layer match and a human accepting an OCR reading are different evidence, and OCR can be wrong in ways a text-layer match cannot. Merging them makes the product claim a certainty it does not have (rules 1 and 2). Every human decision is audited: who, when, from which state to which.

**`accepted_ocr` can still pass the gate.** If a human accepts the OCR reading and confirms non-expired evidence, the criterion is `Pass` and the UI says "Pass - OCR citation, human-confirmed". The distinction is preserved in the label; it is not silently promoted to `verified`.

**No roles.** The API has no admin. Any authenticated user may annotate globally within the rules above (`needs_review`, `rejected`, `accepted_ocr`) and may never erase corpus evidence. A `rejected` requirement stays visible to everyone, badged "flagged by another user"; a company that does not care hides it for itself in `company_requirement_flags`.

**Rule 2 — "rejected" must not mean two different things.** If Company A rejects a requirement because it is *irrelevant to them*, and that is global, the requirement silently disappears for Company B who needs it. So split the two questions:

| Question | Where it lives |
|---|---|
| "Is this extraction real?" | `requirements.state` — global |
| "Do I care about it?" | `company_requirement_flags` — per company |

**And the actual "thorough" part: a re-validation sweep.** Because shared errors propagate, verification must be continuously re-provable, not a one-time checkbox. A cheap scheduled job re-runs the citation gate over every `verified` requirement against the current page text. It catches: a document replaced with shifted pagination, an extraction bug that shipped verified citations, and an OCR page that later gained a real text layer. It is a string comparison per requirement — seconds.

## 6.6 The four levels of identity

Four different things get called "the tender changed". Keeping them apart is what stops both duplicate rows and lost history.

| # | Concept | Shape | Example |
|---|---|---|---|
| 1 | **Tender identity** | one row per *notice* | `2026_NHAI_295179_1` |
| 2 | **Tender history** | many snapshots *inside* one row | same tender, closing moved 5 → 12 Nov |
| 3 | **Document version** | `v1`, `v2` *inside* one document | RFP re-uploaded, pagination changed |
| 4 | **Tender lineage** | a pointer *between two rows* | corrigendum `…_2` supersedes `…_1` |

The practical difference, which is the point of separating them:

| Situation | User must | System must |
|---|---|---|
| **2** — tender changed | keep working; the deadline moved | diff snapshots, reopen reminders, **no new documents** |
| **3** — document changed | re-read that one PDF | new version, re-embed, retire old vectors, invalidate answers |
| **4** — corrigendum | **re-download the document set** | new row, new fetch job (new captchas), show "amended from …" |

Treating a corrigendum as level 3 means nobody ever fetches the corrected documents. Treating it as a duplicate means two rows competing for attention with different deadlines — the exact failure the date rules exist to prevent.

**The deciding rule:** same tender id → **document version** (3). New tender id → **new row with lineage** (4).

## 6.7 Corrigenda and lineage

A corrigendum is the department's official correction published after the original. It changes something material — closing date, EMD, quantity, eligibility.

| | Original | Corrigendum |
|---|---|---|
| Portal tender id | `…_1` | `…_2` ← **different** |
| Reference | `NHAI/HQ/2026/114` | `NHAI/HQ/2026/114` ← **same** |
| Closing | 5 Nov 2026 | **12 Nov 2026** |

Two rows, one nullable column pointing the newer at the older:

```
tenders
  id                     t_A                 t_B
  portal_tender_id       …_1                 …_2
  reference              NHAI/HQ/2026/114    NHAI/HQ/2026/114
  closing_at             2026-11-05          2026-11-12
  supersedes_tender_id   NULL                → t_A
```

What it buys:

- **The list shows one tender** — the live one, badged *"Amended: closing moved 5 → 12 Nov"*. No stale duplicate to click by accident.
- **Nothing is deleted.** The original stays for audit and for anyone who saved it before the amendment.
- **The diff is visible** through the existing snapshot machinery, which is what the Changes tab is for.
- **Reminders move**, because deadlines derive from the live row.

**Why not merge them into one row?** They are two notices with two identities. Merging destroys the history of what the deadline used to be.

**Detecting them, carefully.** A reused reference is a *signal, not proof*:

1. **Strongest:** the portal itself states "Corrigendum to tender X" → link immediately.
2. **Strong:** same portal + normalised reference + organisation + work title + later publish date → **propose** the link, mark `possible_corrigendum`, let a human confirm. Never auto-link on reference alone.
3. **Not a corrigendum:** same tender id but new content → that is a document version (level 3), not a new row.

## 6.8 Qdrant — deferred

**Not part of the near-term build.** It arrives only when §5.11 resumes. The shape below is retained so the deferred work has a target.

One collection per embedding model version, e.g. `tender_chunks_bge_small_v1`:

```
point id     = uuid5(document_id, version, page, chunk_index)   ← deterministic
vector       = 384 floats (model-dependent)
payload      = { document_id, tender_id, page, version, content_hash, section? }
```

Deterministic ids make re-indexing an **upsert**, not an insert — otherwise a rebuild duplicates points and search returns the same passage three times with three scores. With embedded Qdrant there are no aliases, so the active collection is recorded in `app_state` instead (Part 7.7).

## 6.9 A worked example, end to end

```
GLOBAL — public facts
tenders
  id                      t_8f2a…
  portal_id               cppp
  portal_tender_id        2026_NHAI_295179_1
  reference               NHAI/HQ/2026/114
  authority               NHAI
  work_title              "Road construction, NH-48, 12 km"
  closing_at              2026-11-05T12:00:00+05:30
  value                   8,40,00,000
  location                Maharashtra
  extracted_from          listing_table
  unverified              ["value"]          ← claimed, page did not state
  supersedes_tender_id    NULL
        │
        ├── tender_locators      (how to find it again)
        ├── tender_covers        v1: 1 EMD / 2 RFP / 3 BOQ / 4 Integrity Pact / 5 Annexures
        ├── document_fetch_state 4 of 5, missing cover 3
        ├── tender_documents     EMD.pdf (41 pp), RFP.pdf (87 pp) …
        ├── requirements         r_1 "Bid security of ₹8,40,000 in favour of NHAI"
        │                             p12, cl 4.2, verified
        ├── tender_snapshots     s1 @ t0, s2 @ t1
        └── changes              "closing_at 2026-11-05 → 2026-11-12"

PER COMPANY — private judgement
company_tenders     saved_at, bidding_at
evidence_matches    r_1 → none                       → Unknown
fit_assessments     score 78, gate FAIL
checklist_tasks     "Upload EMD ₹8,40,000 in favour of NHAI by 2026-11-05 12:00"
company_requirement_flags  (ignored: r_9)
```

Note the shape: **one tender, many companies.** Adding a second company adds rows only in the lower block.

---

# Part 7 — The chunk / embed / re-index pipeline **[DEFERRED]**

> **Status: deferred with §5.11.** Nothing in this part is built until the question-answering feature resumes. It is kept because the invariants below are what a future implementation must satisfy, and because the "rebuildable from Postgres" guarantee must be designed for from the start even while the vectors are absent.

This is the machinery behind Part 3's "Qdrant is disposable".

## 7.1 Ingest

1. New document attached → compute `sha256`. Duplicate? Stop (this is the existing upload dedupe, reused as the change detector).
2. Extract page text; store pages with viewer index **and** printed label.
3. Split into chunks; store rows with `embed_state = pending`.
4. Worker picks up `pending`, embeds, upserts to Qdrant, sets `done`.
5. When the document's chunk count matches its `done` count → flip `current_version` for that document.

**The embedding queue lives in the queue table in Postgres, not in Qdrant.** If the worker dies, `pending` rows simply retry. This is the concrete reason for "Postgres is the truth".

## 7.2 Document changes to v2 — the correct sequence

1. `v2` detected (different `sha256`).
2. `v1` chunks marked `superseded = true` — **kept, not deleted**. Old answers cite them; you must still be able to show what `v1` said, and to audit.
3. `v2` chunks written `pending`.
4. `v2` embedded, `current_version` flipped.
5. **Retire `v1` vectors from Qdrant.** Search must never return `v1` chunks once `v2` exists — the document changed, answers should use the new text. Re-validating an old answer needs only Postgres text, never vectors.
6. Search filters on `version = current`. Old and new can coexist briefly in Qdrant without ever mixing in results.

> **Retire vectors, keep text.** That one line is what keeps Qdrant bounded over years.

Why not overwrite in place: if you overwrite and crash, search returns a mix of old and new chunks for the same passage — **contradictory citations**, which is worse than stale ones (rule 1).

## 7.3 Drift check

A health endpoint compares, per document: `current_version` chunk count in Postgres vs Qdrant points with that version filter. Mismatch = drift → re-index that document. Report it rather than silently under-searching.

## 7.4 Graceful degradation

If Qdrant is down or empty, fall back to **Postgres full-text / trigram search** over the same chunk table. The app keeps working. Exact-reference lookups (`2026_NHAI_295179_1`) are genuinely better served by full-text anyway, so the fallback is not a disaster mode.

## 7.5 Deletion and backups

- Soft-delete in Postgres → sweep Qdrant points by filter → a reconciler catches anything the sweep missed. Never rely on a naive cascade between two systems.
- Back up Postgres.
- Snapshot Qdrant only for speed. Restoring Postgres without Qdrant must lose nothing but re-index time.

## 7.6 Embedding model choice

- **Local model** (`sentence-transformers`, CPU). Free, offline, and company profiles, evidence and tender text never leave the machine. An embeddings API quietly ships data out — unacceptable here (rule 9, and simple confidentiality).
- **`model_version` on every chunk, from day one.** Change the model and every vector is invalid; the version field makes re-embedding incremental instead of total.
- Dimensions differ between models. Never mix models in one collection.

---

## 7.7 Embedded Qdrant vs server Qdrant

Qdrant runs in two shapes, and the choice matters more than it looks for a local app.

| | **Embedded (local mode)** | **Server mode** |
|---|---|---|
| What it is | The Python client owns a directory on disk, **in-process** | A separate service on a port, talked to over HTTP/gRPC |
| Extra process to run | None | One (Docker or binary) |
| Extra script to write | None | A `start-qdrant` equivalent |
| Concurrency | **One process owns the directory** (file lock) | Many clients |
| Aliases / snapshots / replication | Not available | Available |
| Crash blast radius | Takes the app with it | Independent |
| Suited to | **This project: single-user local app** | Hosting, multiple workers |

**Recommendation: embedded, on disk.** For a local app it removes an entire class of "it didn't start" problems — no second daemon, no port, no Docker, no health check. There is also a `:memory:` variant that keeps nothing after exit; that is excellent for **tests** and wrong for real data.

Two constraints that come with embedded mode, and both must be respected from day one:

1. **Pin the backend to one process.** With embedded Qdrant, running `uvicorn --workers 4` means four processes fighting over one file lock. Use a single worker (or one worker plus a separate job worker that does *not* touch Qdrant). This is a real trap when someone later "optimises" the server config.
2. **No alias-based version switching.** In server mode you flip an alias to point search at the new collection. Embedded, you instead store a pointer in Postgres — `active_collection = "tender_chunks_bge_small_v1"` — and read it at startup. One row, same effect, and it keeps Postgres as the control plane, which suits the rest of this design.

**Switching between modes must be a config change, not a rewrite.** That is guaranteed only if every Qdrant call goes through one thin adapter (see Part 17). And because vectors are derived, the cheapest migration is not a data copy — it is *flip the mode, re-index from Postgres*. That is the payoff for Part 3.3.

## 7.8 Invariants vs implementation choices (for a rewrite)

The new base code may bring its own Qdrant logic. That is fine — but only these **invariants** must survive. Everything else is free to change.

**Invariants (breaking one of these breaks correctness or the "Qdrant is disposable" guarantee):**

1. Chunk **text** and **embedding state** live in Postgres, not in Qdrant.
2. Point ids are **deterministic** from chunk identity, so re-indexing upserts instead of duplicating.
3. Every chunk carries a **version**; the document has a single **current version** pointer.
4. Search filters on **current version** only.
5. Superseded vectors are **retired**; superseded text is **kept**.
6. Qdrant payload is **thin** — no text, no state, no business logic.
7. The whole index is **rebuildable from Postgres alone**.
8. Every cached answer is **re-validated against its citations** before being served.

**Choices (change freely):**

- embedded vs server mode; on-disk vs in-memory (tests only)
- collection naming, and whether the active collection is tracked in Postgres or by alias
- payload field names and whether `section` is stored there
- chunk size and overlap (`CHUNK_CHARS`)
- top-k, whether a reranker is used
- whether the retrieval cache lives in a Postgres table or as a separate "questions" collection
- whether superseded vectors are deleted immediately or after a grace period

If the provided Qdrant code differs from Part 5.11 and Part 7, check it against the eight invariants and nothing else. A rewrite that violates invariant 1 (state in Qdrant) or 7 (not rebuildable) should be rejected regardless of how clean it looks.

---

# Part 8 — End-to-end workflows

Each is written as a timeline. These are the scenarios that justify the whole design.

## W1 — A tender the human never had to touch (scheduled discovery)

```
Wed 09:00  app opens; last_completed_run_at = Mon 22:00 → 35h → overdue
Wed 09:00  job "discovery" queued
Wed 09:00  browser lane free → session opens → portal → directory
Wed 09:01  organisation chosen by tiering (road-specific body outranks PWD)
Wed 09:02  list read; 14 rows; 6 pass the gate (work matches, closing in future)
Wed 09:04  notice #1 clicked → read → locator + fields stored → back to list
Wed 09:05  notice #2 … #6 (per-org cap 4 reached for org A → org B)
Wed 09:09  run finishes: 7 tenders listed, 9 rejected with reasons
Wed 09:09  last_completed_run_at = Wed 09:09
Wed 09:10  user opens Discover: instant list, timestamp "scraped 6 min ago"
```

**Nobody answered a captcha.** Listing pages are normally not gated, so the walk is the cheap 90%; if the portal blocks the directory, the walk parks for a human instead of failing the run.

## W2 — The user saves a tender

```
user clicks Save
  → company_tenders row created (the tender itself is global)
  → auto-fetch flag OFF by default
  → tender shows: "Documents: none. Fetch documents."
```

If the company turned on "fetch documents when I mark this as bidding":

```
  → BIDDING marker set → job "fetch_documents" queued
```

Not on "saved" — see W3 for why.

## W3 — Fetching documents, Thursday, days after the scrape

```
Thu 10:00  user clicks "Fetch documents"
Thu 10:00  job queued; browser lane free → fresh session
Thu 10:00  navigate: portal → NHAI → list → row with tender id 2026_NHAI_295179_1
           (Monday's session is long dead — and irrelevant, thanks to the locator)
Thu 10:01  notice opened; 5 document links read (EMD.pdf, RFP.pdf, BOQ.xls, …)
Thu 10:01  EMD.pdf requested → **captcha page arrives**
Thu 10:01  job → needs_you; captcha window opens on the portal's own page
Thu 10:02  human reads the picture, types the answer, submits
Thu 10:02  response = EMD.pdf → attached → job → running
Thu 10:03  RFP.pdf requested → **another captcha** (per-file gating)
Thu 10:03  job → needs_you again … human answers … attached
Thu 10:05  BOQ.xls requested → arrives normally, but is not a PDF
           → reported, not attached
Thu 10:05  bundle.zip requested → arrives → opened → 2 PDFs attached one by one
Thu 10:06  job → partial ("4 of 5 attached; 1 needs your input")
Thu 10:06  requirement extraction auto-triggered for the 4 attached documents
Thu 10:07  job → done
```

**Why nothing was lost:** the session died four times across this story, and each death cost one page-load, because the tender is identified by id, not by URL.

## W4 — From documents to a decision

```
extraction runs (Groq, untrusted)
  → 31 proposals
  → citation gate: 24 verified, 5 needs_review (OCR pages), 2 rejected (not found)
  → human reviews the 5

assessment (deterministic)
  → score 78/100  (profile fit 25, citations 20, evidence 24/40, readiness 15)
  → gate: FAIL  (ISO 9001 expired → confirmed contradiction)
  → UI shows the gate FIRST, then the score

checklist generated (idempotent)
  → "Submit EMD ₹8,40,000 in favour of NHAI by 2026-11-05 12:00"
  → "Attach ISO 9001 valid on 2026-11-05"  ← no valid evidence exists
  → post-award obligations listed separately, each cited
```

## W5 — Asking a question about a document (cache miss, then hit)

> **Deferred.** W5 and W6 describe the parked question-answering feature (§5.11). They are kept because the machinery below is already designed; they are not part of the near-term build.

```
Q: "What is the bid security for this tender?"

1. exact-hash lookup (company, question, doc versions, prompt version) → miss
2. embed question (local model)
3. Qdrant → top 8 chunks (EMD.pdf p12 clause 4.2 scores highest)
4. Postgres → fetch those chunks' text
5. Groq → "Bid security is ₹8,40,000, payable in favour of NHAI." [EMD.pdf p12, cl 4.2]
6. store: answer + citations + version set + prompt/model version
7. store: retrieval entry (question + chunk ids)     ← tier 1, shareable

Q again, same company:
1. exact-hash lookup → hit
2. re-validate the quoted excerpt still exists on EMD.pdf p12 v1  ✓
3. return immediately — no embedding, no vector search, no Groq call

Q from a DIFFERENT company, identical wording:
1. exact-hash (company-scoped) → miss
2. tier 1 retrieval cache → hit  (same public document, same question)
3. compose a fresh answer for their company — or, if the question is a pure
   document fact, serve the tier-3 cached answer after re-validation
```

## W6 — The document changes to v2

```
new RFP.pdf uploaded (different sha256)
  → v2 pages + chunks stored (pending → done)
  → current_version = v2
  → v1 chunks superseded (kept for history/audit)
  → v1 vectors retired from Qdrant
  → every cached answer pinned to v1 → invalidated
next question → regenerated against v2, cites v2 page numbers
old answers still displayable with their v1 citations intact, labelled "superseded"
```

## W7 — The tender itself changes

```
scheduled recheck → new snapshot
  → content hash (fields + requirement fingerprints) differs
  → deterministic diff → changes: closing date moved 2026-11-05 → 2026-11-12
  → Change appears in the tender's Changes tab and the global Changes page
  → deadline reminder reopens with the new date
  → snapshot times are strictly increasing (stepped by 1 ms, id tie-breaker),
    so "the latest snapshot" can never be an older one
```

## W8 — A corrigendum arrives

```
Thu 09:05  scheduled run reads a new notice: portal tender id …_2
           same reference NHAI/HQ/2026/114, same organisation, closing 12 Nov
Thu 09:05  rule fires: reference reused + later publish date + same work title
           → proposal, not a merge: "possible corrigendum to …_1"
Thu 09:06  human confirms in the tender view
Thu 09:06  new row created with supersedes_tender_id = …_1
Thu 09:06  Discover shows ONE tender (…_2), badged "Amended from …_1"
Thu 09:06  document fetch state on …_2 = "0 documents, 5 covers expected"
           → documents must be re-downloaded: the original's PDFs are the OLD terms
Thu 09:07  the diff …_1 vs …_2 appears in Changes: closing 5 → 12 Nov
Thu 09:07  the reminder moves to 12 Nov
```

Contrast with the case that is *not* an amendment:

```
same portal tender id, new document content
  → not a new row
  → new document version (v2) + snapshot, documents kept, deadline unchanged
```

---

# Part 9 — Failure modes and what the user sees

Honesty at the failure boundary is a feature, not an apology.

| Failure | What the system does | What the user sees |
|---|---|---|
| Laptop was off for 3 days | Startup catch-up runs immediately | "Scraped just now" |
| Portal returns "Not Found" for a link | Counts as unread, never stored | Run reports "1 unreadable" |
| Captcha unanswered | Job parks as `needs_you` | "Waiting for your input — 1 document" |
| Human walks away for a day | Job survives; resumes from locator | Same, next time they open the app |
| Captcha wrong | Reported, not looped | "Captcha not accepted — try again" |
| Second captcha on the same file | Stop, report | "Portal asked again; needs another attempt" |
| File is not a PDF | Reported, not attached | "BOQ.xls is not a PDF — download it manually" |
| Document fetch ends with nothing | Says so | "No documents could be fetched" |
| Notice read but has no link | Kept on the run record, not a tender | "Read but unlinked — cannot be cited" |
| Qdrant down | Full-text fallback | Search works, slightly worse |
| Qdrant empty after restore | Re-index job | "Rebuilding search index…" |
| Groq down / 429 | Bounded retries, then report | "Extraction failed — retry" |
| Excerpt not found on cited page | Stored `unverified` with reason | Badge says why |
| OCR-only match | `needs_review` | "Found in scanned text — please confirm" |
| Evidence expired | `Fail`, not `Unknown` | "ISO 9001 expired on 2026-09-30" |
| No evidence found | `Unknown` | "No evidence on file — not a pass" |
| Nothing listed in a run | States *why* (read nothing / unlinked / filtered) | A run that read-and-discarded never looks like a run that found nothing |
| Cover listed but no file exists | Report it, keep the tender complete-with-a-note | "Cover 4 (Integrity Pact) has no downloadable document" |
| Fewer files than covers | Name the gaps | "4 of 5 — cover 3 (BOQ) not yet fetched" |
| More files than covers | Accept, attach all | "6 files, 5 covers — extra: corrigendum.pdf" |
| A corrigendum appears | New row linked to the original | "Amended: closing moved 5 → 12 Nov — re-download documents" |
| Same tender re-scraped tomorrow | Upsert, no duplicate | One row; "updated 6 min ago" |
| Company A rejected a requirement as irrelevant | Per-company flag only | Company B still sees it |
| A `verified` requirement stops matching its page | Re-validation sweep flags it | "Citation no longer matches — needs review" |

---

# Part 10 — Scale math (why this does not explode)

The question "will this get too big?" deserves numbers.

**Answers and cache rows: hundreds to thousands. Irrelevant.** Postgres handles millions without noticing. Growth here is driven by *people asking questions*, which is slow.

**Chunks are the only real growth, and they grow with documents, not questions.**

```
1 tender PDF, 40 pages        ≈ 100–200 chunks
4 documents per tender        ≈ 500 chunks
Postgres text per tender      ≈ 0.5 MB
Vectors (384-dim f32)         ≈ 0.75 MB
                               ─────────
per fully-ingested tender     ≈ 1.3 MB
```

| Tenders with documents | Postgres text | Qdrant |
|---|---|---|
| 300 | ~150 MB | ~225 MB |
| 1,000 | ~500 MB | ~750 MB |
| 5,000 | ~2.5 GB | ~3.7 GB |

**Gigabytes, not terabytes.** And the biggest lever is free: **only ingest documents for tenders the user intends to bid on.** Discovery can find 10,000 tenders and cost zero vectors.

Four more levers:

1. **Don't store text twice.** Page text already exists; chunks reference it. Duplicated text is usually the real bloat, not the vectors.
2. **Thin Qdrant payload.** No text in Qdrant.
3. **Exact-hash lookup before vector search**, so most repeat questions never touch Qdrant at all.
4. **Retire superseded vectors.** Old text stays in Postgres; old vectors go.

---

# Part 11 — Configuration

Existing knobs, unchanged in meaning:

| Area | Knobs |
|---|---|
| **Walk** | `AGENT_WALK_MAX_PAGES` 5, `AGENT_WALK_MAX_NOTICES` 10, `AGENT_WALK_MAX_NOTICES_PER_ORG` 4, `AGENT_WALK_MAX_ORG_PAGES` 8, `AGENT_WALK_MAX_ATTEMPTS` 3 |
| **Discovery** | `DISCOVERY_MAX_TENDERS` 25, `DISCOVERY_BATCH_SIZE` 5, `DISCOVERY_MIN_SCORE` 0.3, `DISCOVERY_CACHE_HOURS` 24, `DISCOVERY_SEARCH_ENABLED` off, `DISCOVERY_RESET_LIST_ON_RUN` off |
| **Agent** | `AGENT_MAX_STEPS` 0, `AGENT_MAX_DURATION_SECONDS` 0 (**0 = omit the limit** — not every account is enabled and it is refused with 403), `AGENT_CHALLENGE_WAIT_SECONDS` 600, `AGENT_PROXY_COUNTRY_CODE` IN |
| **OCR** | `OCR_ENABLED` true, `OCR_LANGUAGE` eng, `OCR_DPI` 300, `OCR_MAX_PAGES` 40, `OCR_TIMEOUT_SECONDS` 45 |
| **AI/PDF** | `AI_PROVIDER`, `GROQ_API_KEY`, `GROQ_MODEL` `openai/gpt-oss-120b`, `PDF_MAX_PAGES` 200, `AI_EXTRACTION_MAX_CHUNKS` 12 |
| **Email/OTP** | `SMTP_*`, `OTP_EXPIRE_MINUTES` 10, `OTP_RESEND_COOLDOWN_SECONDS` 60, `OTP_MAX_ATTEMPTS` 5 |

**[PLANNED] new knobs:**

| Knob | Default | Meaning | Status |
|---|---|---|---|
| `SCRAPE_INTERVAL_HOURS` | 24 | **and** the catch-up threshold on startup | near-term |
| `DOC_FETCH_AUTO_ON` | false | auto-fetch documents for tenders marked *bidding* | near-term |
| `DOC_FETCH_MAX_PARALLEL` | 1 | browser lane width (keep at 1) | near-term |
| `EMBED_MODEL` | `bge-small-en-v1.5` | local model | **deferred** |
| `EMBED_DIM` | 384 | must match the model | **deferred** |
| `CHUNK_CHARS` | ~1200 | chunk size | **deferred** |
| `RETRIEVAL_TOP_K` | 8 | chunks per question | **deferred** |
| `ANSWER_CACHE_TIERS` | `1,2` | tier 3 opt-in only | **deferred** |
| `ANSWER_ALLOWED_HOSTS` | `*.gov,*.nic` | rule 9, enforced in code | **deferred** |

---

# Part 12 — What this system deliberately does not do

1. **No captcha solving.** Rules 5 and 12. A person answers, every time.
2. **No autonomous bid submission.** It produces a checklist for a human.
3. **No legal-eligibility claims.** `Pass` means "verified or human-confirmed OCR citation + confirmed, non-expired evidence", not "you may lawfully bid".
4. **No fabricated authorities, deadlines or consequences.** If the source does not state it, it is not shown.
5. **No credential collection.** A login wall stops the run; it is never a prompt.
6. **No cross-company answer sharing** except document facts.
7. **No silent degradation.** Every missing piece has a visible state.
8. **No reliance on a vendor for capability.** The browser is Playwright; the vector store is local; the only hosted call is text generation.

---

# Part 13 — Migration path from today's build

> **This part applies only if the current codebase is kept.** The project is now being rebuilt from scratch, so the operative document is **Part 16** (inventory and removal checklist) and **Part 17** (the seams). What remains useful here is the *order of operations* and the honest cost estimate, because the work is the same work — it is just being done in a new tree instead of in place.

Today: MongoDB (`motor`), 12 repository modules (~53 KB), 19 test files using `mongomock`, indexes defined in `app/core/database.py`.

**Step 1 — schema first, code second.** Write the Postgres schema (tables in §6.1) with real foreign keys and unique constraints mirroring the existing index names (`uq_users_email`, `uq_evidence_company_requirement`, `uq_fit_company_tender`, `uq_reminders_company_key`, …). These become enforcement instead of convention.

**Step 2 — swap repositories, not services.** The layering is `routes → services → repositories`, so services should be untouched. If a service needs changing, the repository boundary was wrong.

**Step 3 — re-harness the tests.** This is the underestimated cost: it is not query rewrites, it is the fixture. `mongomock` becomes a real (or ephemeral, e.g. testcontainers) Postgres. Budget for it honestly — 2–4 days total for steps 1–3 is realistic, not an afternoon.

**Step 4 — mechanical conversions.** `ObjectId` → `uuid`; embedded page arrays → `pages` table; the Mongo TTL index on `otp_codes` → an `expires_at` column plus a cleanup job; JSONB for the genuinely variable shapes (`discovery_extracted`, raw rows, snapshots, agent steps).

**Step 5 — keep files on disk.** PDFs are already files. Metadata in Postgres, bytes on disk.

**Step 6 — add Qdrant only when documents exist.** No vectors, no Qdrant. Ingestion is a later phase; do not build a vector store for an empty corpus.

**Step 7 — update the contract.** `AGENTS.md` currently says the database is "decided and locked" as MongoDB, and `docs/PHASES.md` describes the Mongo shape. Changing storage without changing those documents will confuse both humans and agents later.

**Hard rule during migration: do not run both databases.** Mixed storage is worse than either choice.

---

# Part 14 — Build order

> **For the from-scratch rebuild, use Part 19 instead** — it is the same ordering with a testable "done" for each stage. This part is the historical order of the *existing* build, kept because it explains why the features arrived in that sequence.

Do these in order. Each is useful alone.

1. **Locators + durable jobs.** Fixes the "session expired" bug class. No new database needed for jobs — the existing one works; only the state model is new.
2. **Startup catch-up + idempotent discovery.** Makes the 24h schedule actually true. Two hours of work.
3. **Single browser lane + job states.** Removes the class of bug where two things fight over one browser.
4. **Visible-window captcha handoff, one at a time, park-on-abandon.** Then partial states.
5. **Postgres migration.** Do it while the schema is still small (Part 13).
6. **Chunk ingestion + local embeddings + Qdrant**, for saved tenders only.
7. **Ask/answer on top of retrieval**, tier-1 + tier-2 caches, serve-time citation re-validation.
8. **Document versioning → v2 → retire old vectors → cache invalidation.** The machinery in Part 7 is only worth building once documents are actually changing.
9. **Tier-3 document-fact cache**, only after the classifier proves reliable.

---

# Part 15 — Decision log

Every significant choice, the alternative that lost, and why — so the rewrite does not relitigate them. If someone wants to reverse one, this is the argument they must beat.

| # | Decision | Rejected alternative | Why |
|---|---|---|---|
| 1 | **Postgres is the source of truth** | Stay on MongoDB | Multi-step background writes need transactions; relations need foreign keys; dedupe belongs in constraints, not `if` statements |
| 2 | **Qdrant is derived and disposable** | Treat the vector store as a second database of record | A second source of truth needs two-phase consistency; a rebuildable index needs none |
| 3 | **Groq only phrases** | Let the model decide what is true | Rule 6. Any design where the model's output is stored unvalidated fails rule 1 |
| 4 | **Local embeddings** | Hosted embeddings API | Company profiles, evidence and tender text must not leave the machine; also free and offline |
| 5 | **Embedded Qdrant, on disk** | Server mode with Docker | A local app should not require a second daemon; embedded costs only "pin to one process" |
| 6 | **Locators, never URLs** | Store the notice link | The portal's URL carries a session token that dies in minutes (§5.4) |
| 7 | **Durable jobs in Postgres** | Daemon threads only | The laptop sleeps; in-memory work evaporates (§5.5) |
| 8 | **24h schedule with startup catch-up** | A midnight timer | A timer only fires while the app runs (§5.6) |
| 9 | **Headed browser, human answers in the portal's page** | Reconstruct the form post inside our UI | The browser submits natively and correctly; the human visibly does the work (rule 5) |
| 10 | **One captcha window, one browser lane** | Parallel windows | One human cannot service two challenges; parallel windows also fight over session-bound pages |
| 11 | **Park, don't fail, on abandonment** | Fail the job and retry from scratch | Re-finding by locator is cheap; redoing a 10-minute walk is not |
| 12 | **Auto-fetch only for tenders marked "bidding"** | Auto-fetch on every save | Saving 15 tenders would mean 75 captcha prompts — captcha fatigue kills adoption |
| 13 | **Cache evidence across tenants; conclusions within one** | Share answers across companies | Sources being public does not make a company's assessment public (§5.11) |
| 14 | **Re-validate every cached citation before serving** | Trust the cache blindly | Silent staleness is worse than a slow answer (rule 1) |
| 15 | **Version-pinned cache entries** | Validate old answers against current text | Validating `v1` citations against `v2` text is a guaranteed false alarm |
| 16 | **Retire superseded vectors, keep superseded text** | Keep every version's vectors | Old text is needed for citations and audit; old vectors are dead weight (§7.2) |
| 17 | **Deterministic point ids** | Random ids per index run | Re-indexing must upsert, not duplicate (§6.2) |
| 18 | **`.gov` / `.nic` only for answers, with citations** | Answer from anywhere | A confident wrong legal claim is the most expensive failure this product can have (rule 9) |
| 19 | **Scraping is a separate module behind nine contracts** | Scraping built into the base | The base must run and be useful without a browser (§17) |
| 20 | **TinyFish removed** | Keep the vendor for browser hosting | It provided a browser, an IP and session hosting — none of which Python lacks; the browser is Playwright |
| 21 | **Checklist generation is idempotent** | Regenerate from scratch each time | Humans set status and owner; regeneration must never wipe their work (§5.10) |
| 22 | **Partial is a first-class state** | All-or-nothing document fetch | "4 of 5 documents" is the honest, useful outcome (§5.7) |
| 23 | **Score and gate never merge** | One combined number | A high score must never hide a failed mandatory criterion (rule 3) |
| 24 | **24h cadence, not 6-hourly** (for now) | Shorter interval | Staleness only affects tenders closing today; fix that with a visible timestamp, not with more scraping |
| 25 | **Global corpus + per-company judgement** | Per-company copies of every tender | Two copies means two document sets and two captcha bills for the same public facts (§6.1) |
| 26 | **Portal tender id is the identity; the reference is only indexed** | Unique on reference number | References repeat across divisions, drift in punctuation, and are reused by corrigenda and re-tenders (§6.2) |
| 27 | **Dedupe is an upsert, not a skip** | Drop rows already seen | Skipping means never noticing that the deadline moved (§6.2) |
| 28 | **Document fetch state is global** | Track "3 of 5" per company | Otherwise a second company re-answers captchas already answered (§5.7) |
| 29 | **Cover table is a hint, a checklist source and a change signal** | Treat it as the author of "complete" | It is the portal's claim, not ground truth; it still names gaps and detects amendments cheaply (§5.7) |
| 30 | **Corrigenda are new rows with a lineage pointer** | Merge into the existing tender, or dedupe the reference | Merging loses history; deduping hides that the documents must be re-downloaded (§6.7) |
| 31 | **`verified` is set only by the citation gate; `accepted_ocr` stays distinct** | Let a human mark things verified | A human click must never manufacture certainty, and OCR is weaker evidence than a text layer (§6.5) |
| 32 | **Question answering is parked; Qdrant deferred with it** | Build RAG alongside the core | Too many open conditions at once; vectors for a corpus that does not exist are waste (§5.11) |
| 33 | **Answers take 👍/👎, not review states — but silence is not approval across companies** | Verified/rejected workflow for answers, or trust the cache | Rigour scales with blast radius; and a silently wrong shared answer reaches everyone (§5.11) |

---

# Part 16 — The rebuild: what we receive, what we build, what we delete

> **Context.** The product is being rebuilt from scratch. The base code we are given contains **no web scraping** at all, **some** Qdrant logic, and **no Postgres** — and the current MongoDB layer is deliberately removed. This part is the working inventory for that situation.

## 16.1 Ground rules for the rewrite

1. **Postgres is the only relational store.** No MongoDB driver, no `mongomock`, no `ObjectId` anywhere in the new tree.
2. **Scraping is not part of the base.** It is a later, separable module. The base must install, run and pass its tests *with the scraping module absent*.
3. **Qdrant arrives partially built.** Treat it as one candidate implementation. It is judged against the eight invariants in §7.8 — not against the old code.
4. **TinyFish is gone permanently.** No capability in this design depends on a vendor: the browser is Playwright, the vector store is local, the only hosted call is text generation.
5. **Nothing is copied blindly from the old tree.** Every behaviour is re-derived from this document (and the traps in Part 18) rather than ported by habit.

## 16.2 Inventory

| Area | In today's tree | Expected in the provided base | Rebuild action |
|---|---|---|---|
| Auth + email OTP | yes | likely | port hashing/single-use/rate limits; **change to generic 202 signup, create on verify, DB-authorised tenancy** |
| Company profile | yes | likely | port |
| Evidence library (with expiry) | yes | likely | port; expiry is what makes `Fail` possible |
| Tender storage | yes, MongoDB | unknown | **rebuilt on Postgres in stage D** |
| Discovery / portal walk | yes, Playwright | **no** | later, as a separate module behind Part 17 |
| TinyFish integration | yes | **no** | **delete permanently** |
| Browser driver (CDP) | yes | no | rebuild as local, headed Playwright |
| Captcha handling | yes | no | rebuild with the visible-window design (§5.7) |
| PDF text + OCR | yes | likely | **built in stage D** (PyMuPDF + Tesseract, bounded) |
| AI requirement extraction | yes, Groq | likely | **built in stage D**; per-item validation retained |
| **Citation gate** | yes | likely | **built in stage D — it is the product's spine** |
| Assessment (score + gate) | yes | likely | port as **pure functions** (no DB coupling) |
| Checklist / legal obligations | yes | likely | port |
| Snapshots / change review | yes | likely | port |
| Reminders / ICS | yes | likely | port |
| Qdrant / RAG / Ask | partial | **partial** | **deferred** with §5.11 — validate against §7.8 only when resumed |
| Jobs / scheduler / lanes | **no** (threads only) | no | **built in stage C** (`backend_v2`) |
| Frontend | yes | likely | port; tabs unchanged |

**Note on "pure functions".** The deterministic services — relevance matching, the citation gate, scoring, checklist selection, diffing — should not know about the database at all. They take plain data and return plain data. That is what made them portable in the first place, and it is what makes them testable without a database.

## 16.3 The ten things the base needs *now*, even though scraping is out of scope

These are cheap to design in and expensive to retrofit. They exist so the scraping module can be dropped in later without surgery:

1. `jobs` table + the state machine from §5.5.
2. **Single-lane browser lock** - a `lane_leases` row in the database with an expiry, not an in-memory mutex and not `app_state`.
3. **Generic challenge record with no answer storage** - "a human must act in the visible browser, then signal". `POST /challenge` carries no answer; suspend/resume must work even though nothing raises a challenge yet.
4. **Document attach API** with `sha256` dedupe.
5. **Tender identity/upsert with carry-forward merge** (a second read never empties a field the first read had).
6. **`tender_locators`** table (§5.4) - useless today, indispensable later.
7. **`app_state`** with `last_completed_run_at` (§5.6).
8. **Run records with counters and reasons** (§9's last row: a run that read and discarded must never look like a run that found nothing).
9. **File storage abstraction**, local and content-addressed (§16.6).
10. **A typed settings object** containing the knobs from Part 11.
11. **One module registration point** - the scraper registers `/api/discovery/*` and the document-fetch route; `/api/health` reports `features.discovery=false` until it is installed, and the frontend hides those affordances.

## 16.4 MongoDB removal checklist

Be exhaustive here — a half-removed Mongo layer is worse than either choice.

| Item | Action |
|---|---|
| `app/core/database.py` index creation | delete; replaced by migrations |
| 12 repository modules | rewrite as SQL |
| `ObjectId` as id type | → `uuid`; verify nothing in the frontend assumes a 24-char hex string |
| Embedded arrays (`documents.pages`, `discovery_extracted`, snapshot payloads) | → child tables, or `jsonb` where the shape is genuinely variable |
| Mongo TTL index on `otp_codes` | → `expires_at` column + cleanup job |
| Aggregation pipelines (discovery, workflow repos) | → SQL joins |
| `mongomock` fixtures in 19 test files | → ephemeral real Postgres |
| Deps: `motor`, `pymongo`, `mongomock` | remove; add `psycopg`/`asyncpg` + `SQLAlchemy` + `Alembic` |
| `scripts/start-mongo.ps1`, `.data/mongo`, `scripts/migrate-to-atlas.py` | delete |
| Env: `MONGODB_URI`, `MONGODB_DB`, Atlas notes | → `DATABASE_URL` |
| Docs: `AGENTS.md` ("database … decided and locked" = MongoDB), `docs/DATA_MODEL.md`, `docs/PHASES.md` | rewrite — otherwise humans and agents keep building Mongo |
| Tests asserting Mongo behaviour (`DuplicateKeyError`, `$set`) | → `IntegrityError` semantics and explicit `UPDATE` |
| `$set` partial updates | → explicit `UPDATE`; **carry-forward must be `COALESCE(new, old)`** |

That last row is invariant-level. The "a second read must never empty a field the first read had" rule is easy to implement correctly in a Mongo `$set` and easy to implement *incorrectly* in SQL by writing `NULL` over a good value. Write a test for it explicitly.

## 16.5 Postgres addition checklist

- **One pool, one transaction boundary.** Services receive a repository already bound to a transaction, so a multi-step write is atomic. This is the entire reason for the move (§3.2).
- **Migrations from day one** (`Alembic`), applied on startup in dev and explicitly in tests. No `create_all()` in production paths.
- **Constraints mirroring the intended names**, plus foreign keys with explicit `ON DELETE` behaviour: `uq_users_email`, `uq_evidence_company_requirement`, `uq_fit_company_tender`, `uq_reminders_company_key`, `uq_tenders_portal_identity`, `uq_tenders_dedupe_key`.
- **Unique constraints are the dedupe mechanism**, not application `if` statements: `(portal_id, portal_tender_id)` partial, `dedupe_key`, `(tender_id, sha256)`, plus the evidence/fit/reminder/flag uniques.
- **`timestamptz` everywhere.** Date-only deadlines are anchored at **noon UTC** so the Indian calendar day survives display. Snapshot capture times are strictly increasing with an id tie-breaker (§18, row 13).
- **`jsonb` only for genuinely variable shapes** (`discovery_extracted`, raw listing rows, agent step payloads, snapshot bodies). Anything you filter or sort on gets a typed column.
- **Full-text fallback:** a generated `tsvector` column on `chunks.text` plus a trigram index. This is the "Qdrant is down" path (§7.4) and the better path for exact reference lookups.
- **Ids:** UUIDs (v7 recommended) exposed as opaque strings. Never leak integer sequence gaps to the UI.
- **Tests:** one ephemeral Postgres per test session, truncate between tests, and **never mock SQL**. Mocking a database you are migrating onto defeats the purpose of the migration.

## 16.6 File storage

- **Content-addressed:** `storage/<sha256[0:2]>/<sha256>.pdf`.
- Same bytes → same path → dedupe is free, retries are safe, and nothing is ever overwritten in place.
- Postgres stores the path, the `sha256`, the mime type, the byte count and the page count — **never the bytes**.
- Content-addressing doubles as the **change detector**: a new `sha256` for the same tender document means a new version (`v2`), which drives Part 7's re-index and Part 5.11's cache invalidation.

---

# Part 17 — The seam: the interfaces the scraping layer plugs into

**Principle:** the scraping module must never import ORM models, never touch Qdrant, and never write SQL. It talks to interfaces. Then it can be written months later, in its own folder, and dropped in.

## 17.1 The nine contracts

| Contract | What it must guarantee |
|---|---|
| **TenderWriter** | `upsert_by_identity(identity_key, fields, provenance)`. **Global, no company.** Idempotent. **Merges, never nulls.** Records `extracted_from` and the `unverified` list. |
| **LocatorStore** | `put(...)` / `find(portal, portal_tender_id)`. Stores identity, **not URLs**. |
| **DocumentStore** | `attach(tender_id, filename, bytes, source_url, fetched_at)`. `sha256` dedupe, mime/magic-byte check, size cap, immutable. |
| **JobQueue** | `enqueue(kind, payload, priority)`, `claim_next(lane)`, `heartbeat`, `complete`, `partial(n)`, `fail(reason)`, `park_needs_you`. Survives restart. |
| **BrowserLane** | `acquire()` / `release()`, single slot, **`lane_leases` table**, stale-lease expiry, `reserved_for_challenge`, user-initiated outranks scheduled. |
| **ChallengeService** | `open(kind, image, expires_at, context)` -> id; `signal(id)` (the human acted); `wait_for_resume(id, timeout)` -> `resumed`/`expired`. **No answer argument and no answer storage**; the visible browser owns the interaction. |
| **RunRecorder** | `start(kind)`, `count(metric)`, `note(reason)`, `finish(status)`. Counters are what make an honest run report possible. |
| **SchedulerState** | `last_completed_run_at()` / `mark_completed(utc_now)`. Compared as elapsed hours, written on completion. |
| **Settings** | Typed, env-driven, Part 11's knobs. |

## 17.2 Wiring rule

**The base depends on nothing scraping-shaped. The scraping module depends on the base.** The only connection is one registration point where the module declares its job kinds, its FastAPI routes (`/api/discovery/*`, `POST /api/tenders/{id}/documents/fetch`) and its health flag. If you ever find the base importing a scraper, the seam has been broken - the base will stop being testable without a browser, and `/api/health` will lie about what is installed.

## 17.3 How to verify the seam holds

- The base's dependency list contains no Playwright.
- The base's test suite passes with the scraping package deleted.
- The product works on **uploaded** documents alone (requirements, assessment, checklist) — the Ask feature is parked, so this is proven by the core flow rather than by search.
- Deleting the scraping package leaves zero failing tests and zero import errors.

That third bullet matters commercially: **the product is useful before scraping exists.** Upload a tender PDF by hand and you still get cited requirements, an assessment and a checklist.

---

# Part 18 — Traps not to repeat

Every row below cost real time once. Write each one into the new code as a comment, a test, or a constraint.

| # | Trap | Rule |
|---|---|---|
| 1 | Storing the notice URL | Store a **locator**; the URL carries a session token that dies in minutes |
| 2 | The old `/nicgep/` application | Rewrite to `/eprocure/`; an error page is **unread**, never a tender |
| 3 | CPPP row date order | The **middle** date is the closing; the last is the opening |
| 4 | Publish/open date read as closing | Only a **labelled** closing/submission word counts |
| 5 | "The page mentions captcha" | A captcha requires **both** the answer input **and** the image |
| 6 | Downloading again after solving | The **submit response is the file**; there is nothing left at the link |
| 7 | Re-opening the captcha page for a fresh one | Loses the pending request — one page, one attempt |
| 8 | Assuming one captcha per session | Gating can be **per file** |
| 9 | Showing an agent/model value as fact | Keep it, mark it `unverified`, never cite it |
| 10 | OCR text treated as verified | OCR-only matches are `needs_review` |
| 11 | One number hiding a failed gate | Gate first, score second |
| 12 | Treating "no evidence" as a pass | `Unknown`, always |
| 13 | Two snapshots in the same clock tick | Strictly increasing times, stepped by 1 ms, **plus an id tie-breaker** |
| 14 | Date-only deadline drifting a day | Anchor at **noon UTC** so the IST date survives |
| 15 | Zero or `"NA"` read as a value | No value |
| 16 | One field swallowing the next | A field ends where the next label begins |
| 17 | Fee/cover tables read as tenders | One notice page is **one** tender |
| 18 | Embedded Qdrant with multiple server workers | Pin to **one** process (file lock) |
| 19 | A long job holding the HTTP request | Background job + stored state |
| 20 | Auto-fetch on every save | Captcha fatigue — only for tenders marked **bidding** |
| 21 | Sharing an answer across companies | Document **facts** only; never applicability |
| 22 | Validating a `v1` answer against `v2` text | Version-pin the cache entry |
| 23 | Negotiating with a portal that changed | Detect, screenshot, hand to the human, stop — never loop |
| 24 | Sending a limit the server refuses | Historical (a step/duration of 0 means *omit*, not *send zero*), but the lesson generalises |
| 25 | Fighting headless download quirks | A **headed** browser is the design here, not a workaround |
| 26 | Making the reference number unique | References repeat across divisions and are reused by corrigenda; uniqueness belongs on the **portal tender id** |
| 27 | Skipping a tender because it was seen before | **Upsert** it; skipping means never noticing the deadline moved |
| 28 | Per-company document state | Partial is the **tender's** state, or every company re-pays the captcha |
| 29 | Treating the cover table as ground truth | It is a hint: name gaps with it, never fail a fetch because a cover has no file |
| 30 | Treating silence as approval | Fine within one company, never across companies — thumbs-down must reach every cached copy |
| 31 | Letting a company's "not relevant to me" hide a requirement | Global `state` is "is the extraction real"; per-company flags are "do I care" |
| 32 | Merging a corrigendum into the existing tender row | New id → **new row + lineage**, so the corrected documents actually get fetched |
| 33 | Collapsing OCR acceptance into `verified` | Keep `accepted_ocr` distinct; the two are different evidence |
| 34 | Putting the closing date in `dedupe_key` | A moved deadline would create a second row; dates belong to snapshots |
| 35 | Filtering stored tenders by one company's profile | Store the global corpus; apply relevance **at read time** |
| 36 | A `company_id` on a corpus table | Public facts are shared; only judgement is company-scoped |
| 37 | Keeping a `CAPTCHA_MODE` or an answer payload | The human types in the portal page; the API receives a signal, never an answer |
| 38 | Holding the worker thread during a captcha wait | Release the thread, hold the browser session under a bounded `lane_leases` reservation |
| 39 | Using `app_state` as a lock | It has no expiry; a stale lock blocks the lane forever |
| 40 | Registering the scraper's routes in the base | The base must boot with `features.discovery=false` and no browser installed |
| 41 | `replace=true` deleting AI requirements | Deactivate (`is_active=false`); dependants keep a row that still exists |
| 42 | Pointing evidence at a company document, not a version | New bytes under an old confirmation would silently inherit it |
| 43 | Regenerating a checklist and losing its citation | Copy `document_id`, page, clause, excerpt and URL at generation |
| 44 | Registering with 409 for a known email | Empty the response of existence: same `202`, same sentence, create on verify |
| 45 | Two id spaces for a run (`run_id` and `job_id`) | The job is canonical; the run endpoint is a filtered alias |
| 46 | Showing `accepted_ocr` as `verified` in the UI | Say "Pass - OCR citation, human-confirmed" |
| 47 | Treating a manual upload as a second-class tender | `source_type='manual_upload'`, nullable portal, its own `dedupe_key` |
| 48 | A boolean `is_mandatory` | Use `mandatory_state = mandatory/not_mandatory/unknown`; "not read" is not "not mandatory" |
| 49 | Building an admin role to erase corpus evidence | There is no role; annotation is advisory and audited, and flags are per company |
| 50 | Treating a vanished notice or a passed deadline as a cancelled tender | Only the portal's explicit termination/cancellation statement sets `cycle_state='terminated'`; absence is "unread", never a reason |
| 51 | Showing a terminated cycle's old fields | A terminated tender shows only the fixed message; its authority, reference, value, deadline, documents and tabs are hidden |

---

# Part 19 — Definition of done, stage by stage

The rewrite order. Each stage is shippable on its own, and each has a test that proves it.

**Near-term build (A–F, then I, J, K).** Stages **G** and **H** are **deferred** with the question-answering feature (§5.11) — they are listed below for completeness but are *not* in the current plan.

| Stage | Scope | Done when |
|---|---|---|
| **A. Skeleton** | FastAPI (single worker), Postgres + Alembic, settings, health, local start script | App boots with only Postgres running; migrations apply to an empty database; tests use ephemeral Postgres |
| **B. Identity** | Auth, OTP, company profile, evidence library | Signup → code → verify → login works; register is generic `202`; a code-only user must set a password before disabling codes; an expired certificate cannot satisfy a requirement; cross-tenant reads are 404 |
| **C. Jobs** | Job queue, states, `lane_leases`, scheduler + catch-up | Kill the app mid-job → it resumes. Laptop off 3 days → catch-up fires exactly once. A `needs_you` job releases its worker but holds the lane lease; expiry records `partial` |
| **D. Documents** | Upload, PDF text, OCR fallback, extraction, **citation gate**, document lifecycle | A fabricated excerpt is rejected; an OCR-only match can never be `verified`; `replace=true` deactivates AI rows; one current version per `document_key` |
| **E. Decision** | Assessment, checklist, legal obligations | 92/100 with missing mandatory evidence reads **`Unknown`**; gate renders before score; `assessment_criteria` aggregation is any Fail -> Fail; an accepted OCR citation can pass with the OCR label |
| **F. Change** | Snapshots, diffs, changes page, reminders, ICS, **corrigenda lineage** | Two snapshots in the same millisecond never invert order; a corrigendum creates a linked row, not a duplicate |
| ~~G. Recall~~ | ~~Chunks, local embeddings, Qdrant~~ | **Deferred** with §5.11 |
| ~~H. Ask~~ | ~~Retrieval, Groq phrasing, answer cache~~ | **Deferred** with §5.11 |
| **I. Scraping module** | Portal walk, visible-window challenge handoff, locators, listing gate, dedupe/upsert, cover table | Base still installs and tests green with the module **uninstalled**; a blocked walk parks in the same browser and resumes after Continue; re-running the same day creates no duplicate tenders |
| **J. Document fetch** | Visible captcha window, per-file gating, partial states, cover-named gaps | Kill the app during a captcha wait → the job resumes; "4 of 5, cover 3 missing" is displayed honestly |
| **K. Schedule** | 24h + catch-up, refresh-now | The 4–8 ceiling is explained by configuration on screen, not by mystery |

**Status:** Stages A-F, I, J and K are built in `backend_v2` and covered by 70
tests. Stages G/H remain deferred with §5.11. Stage I adds the visible-window
challenge handoff to the portal walk; Stage J adds the global document fetch,
the visible-browser captcha handoff and the cover-table gap report; Stage K adds
the per-tender source recheck (`POST /api/tenders/{id}/refresh`), while the 24h
catch-up and refresh-now path share the same `scrape_portal` job.

**Order rationale:** C before I, because a scraper without a durable job queue will be rebuilt; I before J, because the locator is what lets a fetch happen days later; F before I, because corrigenda lineage must exist before the walk starts producing amendments.

---

# Part 20 — Glossary

| Term | Plain meaning |
|---|---|
| Captcha | Picture puzzle proving a human is present |
| Carry-forward | A new read fills gaps but never erases a field an earlier read had |
| Catch-up | "Has it been 24h since the last run?" checked on startup |
| Chunk | A few paragraphs stored and searched as one unit |
| Citation gate | The deterministic check that quoted words are really on the cited page |
| Content-addressed | A file is stored under the hash of its own bytes, so identical files share one path |
| Cosine similarity | A number saying how similar two meanings are |
| Deterministic | Rule-based, repeatable, no guessing |
| Embedded (Qdrant) | The vector database runs inside the app process, writing to a local directory |
| Embedding | Numbers representing meaning |
| Headed browser | A real visible browser window |
| Job | Background work with a saved state that survives restarts |
| Locator | How to find a tender again: portal + tender id, not a URL |
| OCR | Reading text out of a scanned image |
| Postgres | Relational database — the source of truth |
| Qdrant | Vector database — derived, disposable recall |
| RAG | Retrieve passages, then let a model phrase an answer from them only |
| Session | A browser's cookie identity with a site; short-lived |
| Snapshot | A versioned capture of a tender's fields and requirement fingerprints |
| Tenant | One company's isolated slice of data |
| Verification | Confirmed by deterministic code, not by a model |
| Verbatim | Word for word |

---

# Appendix A — The five sentences that summarise everything

1. **Discovery is cheap, unattended and scheduled; document acquisition is expensive, human-gated and rare.** Keep them apart and the whole system behaves.
2. **Never store a URL, store a locator** — because the portal's session dies in minutes and the tender lives for weeks.
3. **Postgres is the truth, Qdrant is disposable, Groq only phrases.** Lose Qdrant and you lose time, not data.
4. **Cache the evidence across companies, the conclusion only within one** — and re-validate every cached citation before serving it.
5. **Nobody solves a captcha by machine, nothing is cited that cannot be shown verbatim on a real page, and no score is allowed to hide a failed mandatory criterion.**

---

# Appendix B — Discussion log

Dated trail of the design conversations: what was argued, what won, and where it landed. This exists so a rejected alternative is not re-proposed, and so nobody has to read a chat transcript to find out why.

**The transcript itself is history, not spec.** It contains superseded decisions. If it disagrees with this document, **this document wins.** See [`archive/DISCUSSION-TRANSCRIPT.md`](./archive/DISCUSSION-TRANSCRIPT.md).

| Date | Topic | Outcome | Landed |
|---|---|---|---|
| 2026-10-22 | Can plain Python replace the vendor browser? | Yes, except the LLM agent loop, an Indian IP, and a search source | §5.3, §16.1, DL 20 |
| 2026-10-22 | Part-by-part audit of what is portable | Only three gaps; captcha and document plumbing are already plain Python | §16.2 |
| 2026-10-22 | "Python can only read page one" | Category error: executing a known path is Playwright; discovering one is an agent | §5.3 |
| 2026-10-22 | Captcha during document fetch | The captcha page *is* the response; the submission returns the file. Visible browser window chosen | §5.7 |
| 2026-10-22 | Session dies before the user clicks fetch | Locator instead of URL; documents become a durable job | §5.4, §5.5 |
| 2026-10-22 | 4–8 tender ceiling | It is configuration (`AGENT_WALK_MAX_NOTICES`, per-org cap), not a portal property | §5.3 |
| 2026-10-23 | Scheduling in a local app | Startup catch-up: run when the last completed run is older than the interval | §5.6 |
| 2026-10-23 | Postgres vs MongoDB | Move for transactions and referential integrity, not "relational data" | §3.2, §16.4 |
| 2026-10-23 | Postgres + Qdrant + Groq | Endorsed, with Postgres as truth and Qdrant rebuildable and disposable | Part 3, §7 |
| 2026-10-23 | Caching answers across companies | Rejected: leaks assessment and enshrines errors. Cache evidence, not conclusions | §5.11 (reference) |
| 2026-10-23 | "Will this get too big?" | Answers are trivial; chunks grow with documents (~1.3 MB/tender) | Part 10 |
| 2026-10-23 | Full blueprint requested | Written from zero | this document |
| 2026-10-24 | Stage C before Stage I | A scraper without a job queue owns its state in a call stack and gets rebuilt | §5.5, Part 19 |
| 2026-10-24 | "Make the reference unique" | **Rejected**: never unique; portal tender id is identity; dedupe is an upsert | §6.2, DL 26–27 |
| 2026-10-24 | Global corpus | Accepted; document fetch state becomes global and captchas are paid once per tender | §6.1, §5.7, DL 25 |
| 2026-10-24 | Cover table | Adopted as hint, gap-namer, checklist source and change signal — not an arbiter of done | §5.7, DL 29 |
| 2026-10-24 | Answers: thumbs instead of review states | Accepted with limits: keep the liveness check; silence ≠ approval across companies | §5.11, DL 33 |
| 2026-10-24 | Corrigenda | New tender id → new row + `supersedes_tender_id`; same id → document version | §6.6, §6.7 |
| 2026-10-26 | Park the chatbot | Ask deferred, and Qdrant with it; near-term build is Postgres only | §5.11, Part 19 |
| 2026-10-26 | Split blueprint vs coding guide | Split by audience and volatility; every fact in exactly one document | `CODING-GUIDE.md`, `README.md` |
| 2026-10-26 | Docs are the only handoff | Added column-level schema, API surface, enum catalogue and the archive | `SCHEMA-SPEC.md`, `API-SURFACE.md`, this appendix |
| 2026-10-26 | Corpus storage vs listing | Store once, globally, with no profile filter; apply per-company relevance at read time | §6.1, §6.2 |
| 2026-10-26 | Global annotations | Users may annotate (`needs_review`, `rejected`, `accepted_ocr`) and never erase or set `verified`; no roles | §6.5 |
| 2026-10-26 | Dedupe key | No dates: `sha256(portal_id | reference | title | organisation)`; corrigenda are lineage, not duplicates | §6.2 |
| 2026-10-26 | `accepted_ocr` and the gate | Human-confirmed OCR may satisfy `Pass` and is labelled as OCR, never promoted to `verified` | §5.8, §5.9 |
| 2026-10-26 | Auth enumeration | Register/request-code always `202`; user/company created on verification; JWT claim never authorises | §5.1 |
| 2026-10-26 | Captcha and the lane | Visible browser only; no answer storage; `needs_you` holds a bounded `lane_leases` reservation | §5.7 |
| 2026-10-26 | Scraping module | Base installs and tests without it; it registers `/api/discovery/*` and the fetch route; health reports the feature flag | §16.3, Part 17 |
| 2026-10-26 | Requirement schema | Added obligation/deadline/consequence/form fields, `mandatory_state`, `is_navigation`, `is_active`; no hard delete | §5.8, §6.3 |
| 2026-10-26 | Per-criterion gate | `assessment_criteria`; any Fail -> Fail, else any Unknown -> Unknown, else Pass | §5.9 |
| 2026-10-26 | Document lifecycle | `document_key`, `supersedes_document_id`, `extraction_state`; one current version per logical document | §6.3, Part 7 |
| 2026-10-26 | Evidence pinning | Matches point at a `company_document_version_id`, `RESTRICT` + soft delete, recompute on expiry | §6.3 |
| 2026-10-26 | Checklist provenance | Copy the citation at generation; unique per company/tender/requirement/phase | §5.10 |
| 2026-10-26 | Canonical run | The job is the run; `/api/discovery/runs/{id}` is a filtered alias; counters live at `/api/jobs/{id}/runs` | §6.3, `API-SURFACE.md` |

*DL = decision-log row in Part 15.*
