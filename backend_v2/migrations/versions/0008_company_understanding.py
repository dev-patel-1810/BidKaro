"""Company understanding, tender similarity and requirement-experience matches.

An about-company profile is read twice. The deterministic reader keeps work
words (:class:`company_document_terms`, migration ``0007``); this migration
adds the cited *understanding*: the kinds of work the company says it does and
the projects or products it delivered. Every row carries the page and the
verbatim line, and only a human-confirmed capability may be shown to the
similarity judge.

The other two tables store the closed-label answers that use it:

* ``tender_similarity_judgments`` caches one ``yes``/``no``/``unsure`` per
  company and tender, keyed by the inputs that produced it;
* ``requirement_experience_matches`` maps one requirement to the company's own
  experience as ``covered``/``partial``/``new``/``unknown``.

``jobs.kind`` gains ``judge_tender_similarity``; without the constraint change
the enqueue insert fails and the batch action is a 500.

Revision ID: 0008_company_understanding
Revises: 0007_company_document_intel
Create Date: 2026-11-18
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_company_understanding"
down_revision: Union[str, None] = "0007_company_document_intel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE company_documents
            ADD COLUMN IF NOT EXISTS understanding_state text NOT NULL DEFAULT 'pending',
            ADD COLUMN IF NOT EXISTS understanding_error text,
            ADD COLUMN IF NOT EXISTS understanding_at timestamptz,
            ADD COLUMN IF NOT EXISTS capability_count integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS experience_count integer NOT NULL DEFAULT 0;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_capabilities (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
            company_document_id uuid NOT NULL
                REFERENCES company_documents(id) ON DELETE CASCADE,
            name text NOT NULL,
            normalized text NOT NULL,
            description text,
            aliases text[] NOT NULL DEFAULT '{}'::text[],
            page_index integer NOT NULL,
            printed_label text,
            excerpt text NOT NULL,
            quote_verified boolean NOT NULL DEFAULT true,
            is_ocr boolean NOT NULL DEFAULT false,
            state text NOT NULL DEFAULT 'proposed',
            origin text NOT NULL DEFAULT 'ai',
            model text,
            prompt_version text,
            confirmed_by uuid REFERENCES users(id) ON DELETE SET NULL,
            confirmed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_company_capabilities_state
                CHECK (state IN ('proposed','confirmed','rejected')),
            CONSTRAINT ck_company_capabilities_origin
                CHECK (origin IN ('ai','human')),
            CONSTRAINT uq_company_capabilities_document_name
                UNIQUE (company_document_id, normalized)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_capabilities_company_state "
        "ON company_capabilities (company_id, state);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_experiences (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
            company_document_id uuid NOT NULL
                REFERENCES company_documents(id) ON DELETE CASCADE,
            title text NOT NULL,
            normalized text NOT NULL,
            client text,
            description text,
            work_family text,
            value_amount numeric(18,2),
            currency text NOT NULL DEFAULT 'INR',
            completed_on date,
            page_index integer NOT NULL,
            printed_label text,
            excerpt text NOT NULL,
            quote_verified boolean NOT NULL DEFAULT true,
            is_ocr boolean NOT NULL DEFAULT false,
            state text NOT NULL DEFAULT 'proposed',
            origin text NOT NULL DEFAULT 'ai',
            model text,
            prompt_version text,
            confirmed_by uuid REFERENCES users(id) ON DELETE SET NULL,
            confirmed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_company_experiences_state
                CHECK (state IN ('proposed','confirmed','rejected')),
            CONSTRAINT ck_company_experiences_origin
                CHECK (origin IN ('ai','human')),
            CONSTRAINT uq_company_experiences_document_title
                UNIQUE (company_document_id, normalized)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_experiences_company_state "
        "ON company_experiences (company_id, state);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tender_similarity_judgments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
            tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
            input_hash text NOT NULL,
            answer text NOT NULL,
            anchor_text text,
            anchor_verified boolean NOT NULL DEFAULT false,
            needs_confirmation boolean NOT NULL DEFAULT false,
            reason text,
            matched_capability_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
            model text,
            prompt_version text,
            rules_version text,
            judged_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_tender_similarity_judgments_answer
                CHECK (answer IN ('yes','no','unsure')),
            CONSTRAINT uq_tender_similarity_judgments_company_tender
                UNIQUE (company_id, tender_id)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS requirement_experience_matches (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
            tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
            requirement_id uuid NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
            coverage text NOT NULL,
            experience_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
            capability_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
            company_document_id uuid
                REFERENCES company_documents(id) ON DELETE SET NULL,
            company_page_index integer,
            company_excerpt text,
            requirement_excerpt text,
            reason text,
            model text,
            prompt_version text,
            input_hash text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_requirement_experience_matches_coverage
                CHECK (coverage IN ('covered','partial','new','unknown')),
            CONSTRAINT uq_requirement_experience_matches_company_requirement
                UNIQUE (company_id, requirement_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_requirement_experience_matches_company_tender "
        "ON requirement_experience_matches (company_id, tender_id);"
    )

    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN
            ('scrape_portal','sweep_portal','classify_corpus',
             'fetch_documents','refresh_tender',
             'extract_requirements','snapshot_tender',
             'revalidate_citations','send_reminders',
             'extract_company_document','judge_tender_similarity'));
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'judge_tender_similarity';")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN
            ('scrape_portal','sweep_portal','classify_corpus',
             'fetch_documents','refresh_tender',
             'extract_requirements','snapshot_tender',
             'revalidate_citations','send_reminders',
             'extract_company_document'));
        """
    )
    op.execute("DROP TABLE IF EXISTS requirement_experience_matches;")
    op.execute("DROP TABLE IF EXISTS tender_similarity_judgments;")
    op.execute("DROP TABLE IF EXISTS company_experiences;")
    op.execute("DROP TABLE IF EXISTS company_capabilities;")
    op.execute(
        """
        ALTER TABLE company_documents
            DROP COLUMN IF EXISTS understanding_state,
            DROP COLUMN IF EXISTS understanding_error,
            DROP COLUMN IF EXISTS understanding_at,
            DROP COLUMN IF EXISTS capability_count,
            DROP COLUMN IF EXISTS experience_count;
        """
    )
