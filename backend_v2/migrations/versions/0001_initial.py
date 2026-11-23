"""Initial schema — transcription of docs/SCHEMA-SPEC.md with the 2026-10-26
decisions folded in.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCHEMA_SQL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------------------
-- Identity and tenancy
-- ---------------------------------------------------------------------------

CREATE TABLE companies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    region text,
    sectors text[] NOT NULL DEFAULT '{}',
    services text[] NOT NULL DEFAULT '{}',
    keywords text[] NOT NULL DEFAULT '{}',
    profile jsonb NOT NULL DEFAULT '{}',
    is_active boolean NOT NULL DEFAULT true,
    reminder_opt_in boolean NOT NULL DEFAULT false,
    reminder_lead_days integer[] NOT NULL DEFAULT '{7,3,1}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    email citext NOT NULL,
    password_hash text,
    full_name text,
    email_verified boolean NOT NULL DEFAULT false,
    email_otp_enabled boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_email UNIQUE (email)
);

CREATE TABLE company_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    document_type text NOT NULL,
    title text NOT NULL,
    issuer text,
    signatory text,
    issued_on date,
    expires_on date,
    financial_year text,
    project_value_amount numeric(18,2),
    project_value_currency char(3) NOT NULL DEFAULT 'INR',
    notes text,
    verification_status text NOT NULL DEFAULT 'self_declared',
    metadata jsonb NOT NULL DEFAULT '{}',
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_company_documents_type CHECK (document_type IN
        ('registration','iso','turnover','completion_certificate','pan','gst','other')),
    CONSTRAINT ck_company_documents_verification CHECK (verification_status IN
        ('self_declared','verified','rejected'))
);

CREATE TABLE company_document_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_document_id uuid NOT NULL REFERENCES company_documents(id) ON DELETE CASCADE,
    sha256 char(64) NOT NULL,
    storage_path text NOT NULL,
    mime text NOT NULL,
    bytes bigint NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_company_document_versions_sha UNIQUE (company_document_id, sha256)
);

-- ---------------------------------------------------------------------------
-- Global corpus
-- ---------------------------------------------------------------------------

CREATE TABLE portals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL,
    name text NOT NULL,
    base_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_portals_code UNIQUE (code)
);

CREATE TABLE tenders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    portal_id uuid REFERENCES portals(id) ON DELETE RESTRICT,
    portal_tender_id text,
    dedupe_key char(64) NOT NULL,
    reference text,
    reference_normalised text,
    authority text,
    organisation_chain text,
    work_title text,
    work_type text,
    location text,
    region text,
    closing_at timestamptz,
    published_at timestamptz,
    value_amount numeric(18,2),
    currency char(3) NOT NULL DEFAULT 'INR',
    source_type text NOT NULL,
    extracted_from text NOT NULL,
    source_url text,
    retrieved_at timestamptz,
    content_hash char(64),
    unverified text[] NOT NULL DEFAULT '{}',
    supersedes_tender_id uuid REFERENCES tenders(id) ON DELETE SET NULL,
    supersedes_state text,
    merged_into_tender_id uuid REFERENCES tenders(id) ON DELETE SET NULL,
    merged_at timestamptz,
    cross_portal_key char(64),
    raw jsonb,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_tenders_dedupe_key UNIQUE (dedupe_key),
    CONSTRAINT ck_tenders_source_type CHECK (source_type IN
        ('official_portal','authority','aggregator','manual_upload')),
    CONSTRAINT ck_tenders_extracted_from CHECK (extracted_from IN
        ('listing_table','search_result','notice_page','manual_upload')),
    CONSTRAINT ck_tenders_supersedes_state CHECK (supersedes_state IS NULL OR supersedes_state IN
        ('confirmed','proposed','rejected')),
    CONSTRAINT ck_tenders_not_self_supersede CHECK
        (supersedes_tender_id IS NULL OR supersedes_tender_id <> id),
    CONSTRAINT ck_tenders_not_self_merge CHECK
        (merged_into_tender_id IS NULL OR merged_into_tender_id <> id)
);

CREATE UNIQUE INDEX uq_tenders_portal_identity
    ON tenders (portal_id, portal_tender_id)
    WHERE portal_tender_id IS NOT NULL;

CREATE TABLE tender_locators (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    portal_id uuid NOT NULL REFERENCES portals(id) ON DELETE RESTRICT,
    portal_tender_id text,
    reference text,
    organisation_chain text,
    work_title text,
    published_on date,
    last_verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_tender_locators_tender UNIQUE (tender_id)
);

CREATE TABLE tender_covers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    version text NOT NULL,
    cover_no integer NOT NULL,
    doc_type text,
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_tender_covers_version_cover UNIQUE (tender_id, version, cover_no)
);

CREATE TABLE tender_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    filename text NOT NULL,
    sha256 char(64) NOT NULL,
    version text NOT NULL DEFAULT 'v1',
    document_key text NOT NULL,
    supersedes_document_id uuid REFERENCES tender_documents(id) ON DELETE SET NULL,
    is_current boolean NOT NULL DEFAULT true,
    storage_path text NOT NULL,
    mime text NOT NULL,
    bytes bigint NOT NULL,
    page_count integer,
    has_text_layer boolean NOT NULL DEFAULT true,
    ocr_page_count integer NOT NULL DEFAULT 0,
    extraction_state text NOT NULL DEFAULT 'pending',
    extraction_error text,
    source_url text,
    fetched_at timestamptz,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_tender_documents_sha UNIQUE (tender_id, sha256),
    CONSTRAINT uq_tender_documents_version UNIQUE (tender_id, document_key, version),
    CONSTRAINT ck_tender_documents_extraction_state CHECK (extraction_state IN
        ('pending','running','done','failed','no_text_layer'))
);

CREATE UNIQUE INDEX uq_tender_documents_current
    ON tender_documents (tender_id, document_key)
    WHERE is_current AND deleted_at IS NULL;

CREATE TABLE document_fetch_state (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    last_requested_at timestamptz,
    last_attempt_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    expected_covers integer,
    documents_present integer NOT NULL DEFAULT 0,
    missing_covers integer[] NOT NULL DEFAULT '{}',
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_fetch_state_tender UNIQUE (tender_id)
);

CREATE TABLE document_pages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES tender_documents(id) ON DELETE CASCADE,
    page_index integer NOT NULL,
    printed_label text,
    text text NOT NULL DEFAULT '',
    char_count integer NOT NULL DEFAULT 0,
    is_ocr boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_pages_index UNIQUE (document_id, page_index)
);

CREATE TABLE requirements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES tender_documents(id) ON DELETE CASCADE,
    page_index integer NOT NULL,
    printed_label text,
    clause_ref text,
    form_ref text,
    category text NOT NULL,
    phase text NOT NULL DEFAULT 'pre_award',
    title text,
    obligation text,
    deadline_text text,
    deadline_at timestamptz,
    consequence_text text,
    acceptance_criteria text,
    issuer text,
    signatory text,
    evidence_check text,
    excerpt text NOT NULL,
    excerpt_normalised text NOT NULL,
    excerpt_hash char(64) NOT NULL,
    is_navigation boolean NOT NULL DEFAULT false,
    state text NOT NULL,
    state_reason text,
    mandatory_state text NOT NULL DEFAULT 'unknown',
    origin text NOT NULL DEFAULT 'ai',
    model text,
    prompt_version text,
    is_active boolean NOT NULL DEFAULT true,
    reviewed_by uuid REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_requirements_category CHECK (category IN
        ('eligibility','submission','compliance','form','performance')),
    CONSTRAINT ck_requirements_phase CHECK (phase IN ('pre_award','post_award')),
    CONSTRAINT ck_requirements_state CHECK (state IN
        ('verified','needs_review','accepted_ocr','unverified','rejected')),
    CONSTRAINT ck_requirements_mandatory_state CHECK (mandatory_state IN
        ('mandatory','not_mandatory','unknown')),
    CONSTRAINT ck_requirements_origin CHECK (origin IN ('ai','human'))
);

CREATE UNIQUE INDEX uq_requirements_active_excerpt
    ON requirements (document_id, page_index, excerpt_hash)
    WHERE is_active;

CREATE TABLE tender_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL,
    content_hash char(64) NOT NULL,
    fields jsonb NOT NULL DEFAULT '{}',
    requirement_fingerprints jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_tender_snapshots_capture UNIQUE (tender_id, captured_at)
);

CREATE TABLE changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    from_snapshot_id uuid REFERENCES tender_snapshots(id) ON DELETE CASCADE,
    to_snapshot_id uuid NOT NULL REFERENCES tender_snapshots(id) ON DELETE CASCADE,
    change_type text NOT NULL,
    field_key text NOT NULL,
    old_value text,
    new_value text,
    detected_at timestamptz NOT NULL DEFAULT now(),
    review_state text NOT NULL DEFAULT 'pending',
    reviewed_at timestamptz,
    reviewed_by uuid REFERENCES users(id) ON DELETE SET NULL,
    source_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_changes_type CHECK (change_type IN
        ('field_changed','requirement_added','requirement_removed',
         'requirement_changed','document_added','lineage_linked')),
    CONSTRAINT ck_changes_review_state CHECK (review_state IN
        ('pending','acknowledged','dismissed'))
);

CREATE UNIQUE INDEX uq_changes_natural
    ON changes (tender_id, from_snapshot_id, to_snapshot_id, field_key)
    NULLS NOT DISTINCT;

-- ---------------------------------------------------------------------------
-- Per-company judgement
-- ---------------------------------------------------------------------------

CREATE TABLE company_tenders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    saved_at timestamptz,
    dismissed_at timestamptz,
    bidding_at timestamptz,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_company_tenders UNIQUE (company_id, tender_id)
);

CREATE TABLE company_requirement_flags (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    requirement_id uuid NOT NULL REFERENCES requirements(id) ON DELETE RESTRICT,
    ignored boolean NOT NULL DEFAULT false,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_company_requirement_flags UNIQUE (company_id, requirement_id)
);

CREATE TABLE evidence_matches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    requirement_id uuid NOT NULL REFERENCES requirements(id) ON DELETE RESTRICT,
    company_document_version_id uuid NOT NULL
        REFERENCES company_document_versions(id) ON DELETE RESTRICT,
    status text NOT NULL,
    reason text,
    confirmed_by uuid REFERENCES users(id) ON DELETE SET NULL,
    confirmed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_evidence_matches UNIQUE (company_id, requirement_id),
    CONSTRAINT ck_evidence_matches_status CHECK (status IN
        ('covered','missing','expired','contradicted'))
);

CREATE TABLE assessment_criteria (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    requirement_id uuid NOT NULL REFERENCES requirements(id) ON DELETE RESTRICT,
    criterion_text text NOT NULL,
    verdict text NOT NULL,
    reason text,
    evidence_match_id uuid REFERENCES evidence_matches(id) ON DELETE SET NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_assessment_criteria_verdict CHECK (verdict IN ('pass','fail','unknown'))
);

CREATE UNIQUE INDEX uq_assessment_criteria_active
    ON assessment_criteria (company_id, tender_id, requirement_id)
    WHERE is_active;

CREATE TABLE fit_assessments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    score numeric(5,2) NOT NULL,
    components jsonb NOT NULL DEFAULT '{}',
    gate text NOT NULL,
    gate_reason text,
    assessed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_fit_assessments UNIQUE (company_id, tender_id),
    CONSTRAINT ck_fit_assessments_score CHECK (score >= 0 AND score <= 100),
    CONSTRAINT ck_fit_assessments_gate CHECK (gate IN ('pass','fail','unknown'))
);

CREATE TABLE checklist_tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    requirement_id uuid REFERENCES requirements(id) ON DELETE RESTRICT,
    phase text NOT NULL DEFAULT 'pre_award',
    title text NOT NULL,
    category text,
    acceptance_criteria text,
    issuer text,
    signatory text,
    evidence_check text,
    due_at timestamptz,
    trigger text,
    consequence text,
    document_id uuid REFERENCES tender_documents(id) ON DELETE RESTRICT,
    page_index integer,
    clause_ref text,
    excerpt text,
    source_url text,
    status text NOT NULL DEFAULT 'open',
    owner text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_checklist_tasks_natural UNIQUE
        (company_id, tender_id, requirement_id, phase),
    CONSTRAINT ck_checklist_tasks_phase CHECK (phase IN ('pre_award','post_award')),
    CONSTRAINT ck_checklist_tasks_status CHECK (status IN
        ('open','in_progress','done','blocked','not_applicable'))
);

CREATE TABLE reminders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    tender_id uuid REFERENCES tenders(id) ON DELETE CASCADE,
    task_id uuid REFERENCES checklist_tasks(id) ON DELETE CASCADE,
    key text NOT NULL,
    due_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open',
    sent_at timestamptz,
    acknowledged_at timestamptz,
    lead_days integer,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_reminders_company_key UNIQUE (company_id, key),
    CONSTRAINT ck_reminders_state CHECK (state IN ('open','sent','dismissed'))
);

CREATE TABLE audit_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE RESTRICT,
    actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    before jsonb,
    after jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Work and system
-- ---------------------------------------------------------------------------

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE SET NULL,
    kind text NOT NULL,
    lane text,
    state text NOT NULL DEFAULT 'queued',
    priority integer NOT NULL DEFAULT 100,
    payload jsonb NOT NULL DEFAULT '{}',
    idempotency_key text,
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    lease_owner text,
    lease_expires_at timestamptz,
    run_after timestamptz NOT NULL DEFAULT now(),
    last_error text,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_jobs_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_jobs_lane CHECK (lane IS NULL OR lane IN ('browser')),
    CONSTRAINT ck_jobs_state CHECK (state IN
        ('queued','running','needs_you','done','partial','failed','cancelled')),
    CONSTRAINT ck_jobs_kind CHECK (kind IN
        ('scrape_portal','fetch_documents','extract_requirements',
         'snapshot_tender','revalidate_citations','send_reminders'))
);

CREATE TABLE runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE SET NULL,
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    kind text NOT NULL,
    status text NOT NULL,
    metrics jsonb NOT NULL DEFAULT '{}',
    notes text,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_runs_status CHECK (status IN
        ('queued','running','needs_you','done','partial','failed','cancelled'))
);

CREATE TABLE challenges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
    run_id uuid REFERENCES runs(id) ON DELETE CASCADE,
    kind text NOT NULL,
    image_path text,
    context jsonb NOT NULL DEFAULT '{}',
    state text NOT NULL DEFAULT 'open',
    opened_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    answered_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_challenges_kind CHECK (kind IN ('captcha','login_wall','otp')),
    CONSTRAINT ck_challenges_state CHECK (state IN
        ('open','answered','expired','abandoned'))
);

CREATE TABLE lane_leases (
    name text PRIMARY KEY,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    acquired_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    reserved_for_challenge boolean NOT NULL DEFAULT false,
    expires_at timestamptz NOT NULL
);

CREATE TABLE app_state (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE otp_codes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    email citext NOT NULL,
    purpose text NOT NULL,
    code_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_otp_codes_purpose CHECK (purpose IN ('signup','login'))
);

CREATE TABLE discovery_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url text NOT NULL,
    kind text NOT NULL,
    outcome text NOT NULL,
    reason text,
    listing_url text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_discovery_candidates_kind CHECK (kind IN ('source','tender_candidate')),
    CONSTRAINT ck_discovery_candidates_outcome CHECK (outcome IN
        ('stored','rejected','unreadable','unlinked'))
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX ix_users_company ON users (company_id);
CREATE INDEX ix_company_documents_type ON company_documents (company_id, document_type);
CREATE INDEX ix_company_documents_expiry ON company_documents (company_id, expires_on);

CREATE INDEX ix_tenders_closing ON tenders (closing_at);
CREATE INDEX ix_tenders_portal_published ON tenders (portal_id, published_at DESC);
CREATE INDEX ix_tenders_reference ON tenders (reference_normalised);
CREATE INDEX ix_tenders_content_hash ON tenders (content_hash);
CREATE INDEX ix_tenders_supersedes ON tenders (supersedes_tender_id);
CREATE INDEX ix_tenders_merged_into ON tenders (merged_into_tender_id);
CREATE INDEX ix_tenders_cross_portal ON tenders (cross_portal_key);

CREATE INDEX ix_tender_documents_current ON tender_documents (tender_id, version, is_current);
CREATE INDEX ix_requirements_document_state ON requirements (document_id, state);
CREATE INDEX ix_requirements_state ON requirements (state);
CREATE INDEX ix_requirements_document_page ON requirements (document_id, page_index);
CREATE INDEX ix_tender_snapshots_tender ON tender_snapshots (tender_id, captured_at DESC);
CREATE INDEX ix_changes_tender_detected ON changes (tender_id, detected_at DESC);
CREATE INDEX ix_changes_review_state ON changes (review_state);

CREATE INDEX ix_company_tenders_saved ON company_tenders (company_id, saved_at DESC);
CREATE INDEX ix_evidence_matches_requirement ON evidence_matches (company_id, requirement_id);
CREATE INDEX ix_assessment_criteria_tender ON assessment_criteria (company_id, tender_id, verdict);
CREATE INDEX ix_checklist_tasks_due ON checklist_tasks (company_id, due_at);
CREATE INDEX ix_checklist_tasks_tender ON checklist_tasks (company_id, tender_id);
CREATE INDEX ix_reminders_due ON reminders (company_id, state, due_at);
CREATE INDEX ix_audit_logs_company ON audit_logs (company_id, created_at DESC);

CREATE INDEX ix_jobs_state_run_after ON jobs (state, run_after);
CREATE INDEX ix_jobs_lane_state ON jobs (lane, state);
CREATE INDEX ix_jobs_company_created ON jobs (company_id, created_at DESC);
CREATE INDEX ix_runs_company_started ON runs (company_id, started_at DESC);
CREATE INDEX ix_challenges_state_expires ON challenges (state, expires_at);
CREATE INDEX ix_otp_codes_lookup ON otp_codes (email, purpose, created_at DESC);
CREATE INDEX ix_discovery_candidates_run ON discovery_candidates (run_id, kind, outcome);
"""

SEED_SQL = r"""
INSERT INTO app_state (key, value)
VALUES ('last_completed_run_at', 'null'::jsonb)
ON CONFLICT (key) DO NOTHING;

INSERT INTO portals (code, name, base_url)
VALUES ('cppp', 'Central Public Procurement Portal', 'https://eprocure.gov.in')
ON CONFLICT (code) DO NOTHING;
"""

DROP_SQL = r"""
DROP TABLE IF EXISTS discovery_candidates;
DROP TABLE IF EXISTS otp_codes;
DROP TABLE IF EXISTS app_state;
DROP TABLE IF EXISTS lane_leases;
DROP TABLE IF EXISTS challenges;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS reminders;
DROP TABLE IF EXISTS checklist_tasks;
DROP TABLE IF EXISTS fit_assessments;
DROP TABLE IF EXISTS assessment_criteria;
DROP TABLE IF EXISTS evidence_matches;
DROP TABLE IF EXISTS company_requirement_flags;
DROP TABLE IF EXISTS company_tenders;
DROP TABLE IF EXISTS changes;
DROP TABLE IF EXISTS tender_snapshots;
DROP TABLE IF EXISTS requirements;
DROP TABLE IF EXISTS document_pages;
DROP TABLE IF EXISTS document_fetch_state;
DROP TABLE IF EXISTS tender_documents;
DROP TABLE IF EXISTS tender_covers;
DROP TABLE IF EXISTS tender_locators;
DROP TABLE IF EXISTS tenders;
DROP TABLE IF EXISTS portals;
DROP TABLE IF EXISTS company_document_versions;
DROP TABLE IF EXISTS company_documents;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS companies;
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    op.execute(SEED_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
