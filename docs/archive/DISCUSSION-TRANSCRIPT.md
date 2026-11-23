# Discussion transcript — **HISTORY, NOT SPEC**

> ⚠️ **Read the warning before using anything in this file.**
>
> This is a **reconstruction of the design conversations**, written from the working session. It is a chronological record of *what was argued and what was decided* — it is **not** a verbatim export, and it is **not** a source of truth.
>
> **Several decisions in this file were later reversed.** The most important ones:
>
> | Superseded in the transcript | What is actually true |
> |---|---|
> | Tenders are company-scoped, unique on `(company_id, tender_identity_key)` | Tenders are a **global corpus**; company state lives in `company_tenders` (blueprint §6.1) |
> | Make the tender reference number unique | The reference is **never** unique; the portal tender id is the identity (blueprint §6.2) |
> | Answers need `verified` / `wrong` review states and re-evaluation | Answer review is **parked**; lean model with 👍/👎 (blueprint §5.11) |
> | Qdrant and the Ask feature are part of the build | **Deferred** (blueprint §5.11, Part 19) |
> | Keep the vendor browser (TinyFish) for session hosting | **Removed permanently** (blueprint §16.1) |
>
> **If this file contradicts `SYSTEM-BLUEPRINT.md`, the blueprint is right.** Use this file for one purpose only: understanding why an alternative was rejected, so it is not re-proposed.
>
> *To replace this with a verbatim export: save the real transcript over this file and keep the warning header.*

---

## Phase 1 — Can the scraping be done without a vendor?

**Q:** The browser-agent scraping — can normal Python do it, with no API?

**Outcome:** Yes, for everything except three named things. TinyFish supplied a browser, an Indian exit IP, and session hosting — none of which Python lacks. The hardest parts (captcha detection, human handoff, session-bound documents) were already plain Python.

**Three genuine gaps identified:** (1) the autonomous LLM agent loop, (2) an Indian residential IP if running on a server, (3) a source of "what URLs exist" for web search.

**Landed:** blueprint §5.3, §5.7, Part 16.1; decision log #20.

## Phase 2 — Part-by-part audit of "almost all"

**Q:** Name each part and whether it is doable.

**Outcome:** Only the autonomous agent loop (#6), the IP (#4) and the search source (#1) are not plain-Python-doable. Everything else — including 100% of the captcha work and the document plumbing — is Playwright or already written.

**Landed:** blueprint §16.2.

## Phase 3 — "Python can only read page 1"

**Q:** If Python cannot click into a tender and move to page 2, the whole thing is useless.

**Outcome:** A category error worth recording. **Executing a known path** is four lines of Playwright; **discovering an unknown path** is what agents are for. The portal walk already clicked, paginated and returned without any agent.

**Landed:** blueprint §5.3 ("Why it is code and not an agent").

## Phase 4 — The captcha during document fetch

**Q:** Can the captcha page open, the user answer it, the page close, and the code then fetch the file?

**Outcome:** Almost right, with one correction that changes the design: **the captcha page is the response to the file request**, and the file is returned by that submission. There is no "close, then fetch". Three presentation designs reviewed; the **visible browser window** was chosen for a local app, because a human submitting natively is the most robust and visibly satisfies "the software never solves a captcha".

**Landed:** blueprint §5.7.

## Phase 5 — Session expiry between scraping and "fetch documents"

**Q:** By the time the user clicks fetch, the session is dead. Two ideas: (1) scheduled scraping, documents fetched on save by a new daemon; (2) a new session per fetch.

**Outcome:** Option 2 does not work as stated — the stored URL carries a dead session token, so a fresh session still has to *re-find* the tender. Both options therefore share the same mechanism; they differ only in *when the listing runs*. Chosen: option 1's structure with option 2's simplicity — **scheduled discovery, documents as a durable job, locator instead of URL.**

**Also settled:** the 4–8 tender ceiling was **configuration**, not a portal property.

**Landed:** blueprint §5.3, §5.4, §5.5.

## Phase 6 — "Startup catch-up"

**Outcome:** A timer inside a local app only fires while the app runs. The rule became: *on startup, if the last completed run is older than the interval, run now.* Measured as elapsed hours, written on completion, stored in the database.

**Landed:** blueprint §5.6. Appears in the traps list.

## Phase 7 — Postgres instead of MongoDB

**Q:** Move to Postgres for company data?

**Outcome:** Yes, but not because "company data is relational". The real argument is **transactions and referential integrity across multi-step background writes**. Honest cost: 12 repository modules, 19 test files on `mongomock`, 2–4 days. Postgres being free was never the question — the bill is engineering effort.

**Landed:** blueprint §3.2, §16.4, §16.5.

## Phase 8 — Postgres + Qdrant + Groq, and caching answers

**Q:** Relational data in Postgres, vectors in Qdrant, Groq for answers; cache Q&A in Qdrant so the same question is not answered twice.

**Outcome:** The three-store split was endorsed. The **answer cache was challenged**: caching conclusions and serving them across companies leaks one company's assessment and enshrines errors. Recommendation: **cache the evidence, not the conclusion**, with a three-tier model and a document-fact / applicability classifier.

**Landed:** blueprint §5.11 (reference design), §3, decision log #13.

## Phase 9 — "Qdrant rebuildable from Postgres"

**Q:** How does that work in practice?

**Outcome:** Postgres holds chunk text, versions and the embedding **queue state**; Qdrant holds vectors and thin payloads keyed by **deterministic ids**; re-indexing upserts rather than duplicating; superseded vectors are retired while superseded text is kept; search filters on the current version; drift is checked; full-text Postgres is the fallback.

**Landed:** blueprint Part 7, §7.7, §7.8 (the eight invariants).

## Phase 10 — "Will this not be too big?"

**Outcome:** Answers and cache rows are negligible; **chunks grow with documents, not questions**. Roughly ~1.3 MB per fully-ingested tender → hundreds of MB at a realistic local scale. Levers: ingest only tenders you intend to bid on, no duplicated text, thin payloads, exact-hash lookup before vector search, retire old vectors.

**Landed:** blueprint Part 10.

## Phase 11 — Full blueprint requested

**Outcome:** `docs/SYSTEM-BLUEPRINT.md` written from zero: vocabulary, rules, three-store architecture, components, data model, pipelines, workflows, failure modes, scale, configuration, migration, build order, decision log, rebuild inventory, seams, traps, stage DoD, glossary.

## Phase 12 — "Stage C before Stage I" and why

**Outcome:** A scraper built on daemon threads owns its state in a call stack; a scraper built on a job queue owns it in a row. Every later feature — pause for captcha, resume, cancel, partial, progress — is trivial in the second shape and a rewrite in the first. Captcha parking in particular *is* the job state machine. Allowance: spike the portal with a throwaway script, then rebuild on the queue.

**Landed:** blueprint Part 19 (order rationale), §5.5.

## Phase 13 — Table names and the "unique reference number" idea

**Q:** List tables and keys; also make the tender reference unique so tomorrow's scrape does not duplicate.

**Outcome:** **Corrected.** The reference must never be unique: often missing, restarts per division, drifts in punctuation, reused by corrigenda, reused by re-tenders. Identity is the **portal tender id**. Added a `dedupe_key` fallback for rows with no portal id. Also: **dedupe is an upsert, not a skip**, or the deadline change is never noticed.

**Landed:** blueprint §6.2, decision log #26–27, traps #26–27.

## Phase 14 — Global corpus, cover table, thumbs, corrigenda

**Q1:** Empty document tables initially are fine. **Q2:** Answers come straight from passages — drop verified/wrong, use 👍/👎. **Q3:** Explain corrigenda.

**Outcome:**
- **Global corpus** accepted, with the consequence that a captcha is paid **once per tender**, not once per company; therefore document fetch state is global.
- **Thumbs accepted, with limits:** keep the pre-serve citation *liveness* check (it catches staleness, which thumbs cannot), reject uncited sentences, and treat **silence as approval only within one company** — a silently wrong shared answer reaches everyone.
- **Cover table** adopted as a hint, a named-gap source, a checklist source and a cheap change detector — but **not** an arbiter of "done".
- **Corrigenda explained**: new tender id → **new row with `supersedes_tender_id`**; same tender id → document version instead. Four levels of identity written down.

**Landed:** blueprint §5.7, §6.1, §6.5, §6.6, §6.7, §6.9, W8, decision log #25–33, traps #28–33.

## Phase 15 — Park the chatbot

**Outcome:** Question answering is **parked**; chunks, embeddings and Qdrant are deferred with it. The near-term build is Postgres only: skeleton → identity → jobs → documents → decision → change → scraping → document fetch → schedule. The retained RAG design stays in the blueprint as a reference design, clearly marked *do not build*.

**Landed:** blueprint §5.11, Part 7 banner, Part 19.

## Phase 16 — Blueprint vs coding guide

**Outcome:** Split by **audience and volatility**, not by topic. Blueprint = what/why, slow-moving, the contract. Coding guide = how, fast-moving, read every session, and it **links** to blueprint sections instead of restating them. `AGENTS.md` stays a thin entry point. Rule: every fact lives in exactly one document.

**Landed:** `docs/CODING-GUIDE.md`, `docs/README.md`.

## Phase 17 — Docs as the only handoff

**Outcome:** The rebuild happens in a **separate code tree**, so the docs must be self-sufficient. Gaps closed: column-level schema, the HTTP surface, an enum catalogue, a start-of-build brief, and an explicit statement that the implementation is elsewhere. The conversation is archived as non-authoritative history with this warning, plus a discussion log mapping each thread to where its decision landed.

**Landed:** `docs/SCHEMA-SPEC.md`, `docs/API-SURFACE.md`, `docs/README.md`, this file, blueprint Appendix B.
