"""Company brief and cited fit evidence

Revision ID: 0010_company_brief
Revises: 0009_fit_tier
Create Date: 2026-11-22

The brief is the editable, versioned source of the company's core, adjacent,
support and generic work terms. A similarity judgment now keeps the exact line
quoted from each side, the match type and its deterministic evidence rank, so
the UI can show why an answer was given instead of only a label.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0010_company_brief"
down_revision: Union[str, None] = "0009_fit_tier"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_briefs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            version integer NOT NULL,
            is_current boolean NOT NULL DEFAULT false,
            source_document_id uuid REFERENCES company_documents(id) ON DELETE SET NULL,
            source_kind text NOT NULL DEFAULT 'generated',
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            prompt_version text,
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_company_briefs_company_version UNIQUE (company_id, version)
        );
        CREATE INDEX IF NOT EXISTS ix_company_briefs_company_id
            ON company_briefs (company_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_company_briefs_current
            ON company_briefs (company_id) WHERE is_current;
        """
    )
    op.execute(
        """
        ALTER TABLE tender_similarity_judgments
            ADD COLUMN IF NOT EXISTS tender_evidence text;
        ALTER TABLE tender_similarity_judgments
            ADD COLUMN IF NOT EXISTS company_evidence text;
        ALTER TABLE tender_similarity_judgments
            ADD COLUMN IF NOT EXISTS judged_title text;
        ALTER TABLE tender_similarity_judgments
            ADD COLUMN IF NOT EXISTS match_type text;
        ALTER TABLE tender_similarity_judgments
            ADD COLUMN IF NOT EXISTS evidence_rank integer;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tender_similarity_judgments
            DROP COLUMN IF EXISTS evidence_rank;
        ALTER TABLE tender_similarity_judgments
            DROP COLUMN IF EXISTS match_type;
        ALTER TABLE tender_similarity_judgments
            DROP COLUMN IF EXISTS judged_title;
        ALTER TABLE tender_similarity_judgments
            DROP COLUMN IF EXISTS company_evidence;
        ALTER TABLE tender_similarity_judgments
            DROP COLUMN IF EXISTS tender_evidence;
        """
    )
    op.execute("DROP TABLE IF EXISTS company_briefs;")
