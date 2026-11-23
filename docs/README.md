# Docs index

**This repository of docs is the handoff.** The system described here is being rebuilt from scratch in `backend_v2/`. The old `backend/` tree is the behavioural oracle, not the new implementation. Nothing an implementer needs lives only in a chat transcript.

## Read in this order

| # | Doc | Read it when |
|---|---|---|
| 1 | [`../AGENTS.md`](../AGENTS.md) | always first — where you are, commands, and what exists today vs. what is planned |
| 2 | [`SYSTEM-BLUEPRINT.md`](./SYSTEM-BLUEPRINT.md) | before any design decision or any argument about requirements. **The contract.** |
| 3 | [`CODING-GUIDE.md`](./CODING-GUIDE.md) | every coding session — layers, transactions, naming, recipes, tests |
| 4 | [`SCHEMA-SPEC.md`](./SCHEMA-SPEC.md) | when writing models or an Alembic migration |
| 5 | [`API-SURFACE.md`](./API-SURFACE.md) | when writing routes, or when wiring the frontend |
| 6 | [`archive/DISCUSSION-TRANSCRIPT.md`](./archive/DISCUSSION-TRANSCRIPT.md) | only to understand **why** something was rejected. **History, not spec.** |

## The one rule that keeps these honest

**Every fact lives in exactly one document.** The domains are fixed, and a doc is authoritative inside its own domain:

| Doc | Authority |
|---|---|
| `SYSTEM-BLUEPRINT.md` | what/why, rules, flows, decision log |
| `CODING-GUIDE.md` | how the code is written (layers, transactions, tests) |
| `SCHEMA-SPEC.md` | exact columns, constraints, enums |
| `API-SURFACE.md` | exact method, path, shape, status |

If the blueprint's prose disagrees with `SCHEMA-SPEC` or `API-SURFACE` on that
doc's domain, the domain doc wins **and the blueprint is the bug to fix**. A
dated entry in the blueprint's decision log (Appendix B) supersedes earlier
prose anywhere. The archive never wins: if the transcript contradicts the
blueprint, the blueprint is right - several decisions stated confidently in the
conversation were later reversed.

## What is deliberately *not* documented yet

The document question-answering feature (blueprint §5.11), and everything that depends on it: chunks, embeddings, Qdrant, the answer cache. Those are **deferred** and marked so. Do not build them from the retained reference design without re-opening the decision.

## Older docs in this folder (current build only)

`ARCHITECTURE.md`, `DATA_MODEL.md`, `DEPENDENCIES.md` and `PHASES.md` describe the **existing** MongoDB build. They are useful for porting behaviour and for understanding what was already proven, but they are **not** authoritative for the rebuild — storage, the data model and the integration list have all changed.

| File | Status for the rebuild |
|---|---|
| `ARCHITECTURE.md` | superseded by this blueprint |
| `DATA_MODEL.md` | superseded by `SCHEMA-SPEC.md` (MongoDB collections) |
| `PHASES.md` | history of what was built; Part 19 replaces it as the plan |
| `DEPENDENCIES.md` | check against `CODING-GUIDE.md` §2 before trusting it |

## Status at a glance

| Area | Status in `backend_v2` |
|---|---|
| Stage A - skeleton, Postgres, migrations, health | **done**, tests green |
| Stage B - identity, auth/OTP, company profile, evidence library | **done**, tests green |
| Stage C - jobs, `lane_leases`, scheduler, challenge signal | **done**, tests green |
| Stage D - documents, OCR, citation gate, requirement extraction | **done**, 38 tests green at the stage boundary |
| Stage E - assessment, evidence, checklist, legal obligations | **done**, 44 tests green at the stage boundary |
| Stage F - snapshots, changes, reminders, ICS, lineage | **done**, 51 tests green at the stage boundary |
| Stage I - portal walk, dedupe/upsert, locators, listing gate | **done**, 57 tests green at the stage boundary |
| Stage J - document fetch, visible captcha handoff, cover gaps | **done**, 63 tests green at the stage boundary |
| Stage K - 24h catch-up, refresh-now, per-tender source recheck | **done**, 65 tests green at the stage boundary |
| Ask / Qdrant | **deferred** |
