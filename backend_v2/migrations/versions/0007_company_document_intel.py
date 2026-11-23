"""Company-document intelligence: page text and work terms.

An ``about_company`` profile is read deterministically into work phrases and
words, each with the page and the verbatim line it came from. Those terms feed
discovery relevance (never eligibility), so uploading a company profile changes
what the corpus suggests.

The tables are separate from ``company_documents`` because page text and terms
are derived data: deleting a document cascades them away, and the document's
own metadata stays the source of truth. ``jobs.kind`` gains
``extract_company_document``; without it the enqueue insert fails a check
constraint and the upload response is a 500.

Revision ID: 0007_company_document_intel
Revises: 0006_sweep_job_kind
Create Date: 2026-11-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_company_document_intel"
down_revision: Union[str, None] = "0006_sweep_job_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE company_documents
            ADD COLUMN IF NOT EXISTS extraction_state text NOT NULL DEFAULT 'pending',
            ADD COLUMN IF NOT EXISTS extraction_error text,
            ADD COLUMN IF NOT EXISTS extracted_at timestamptz,
            ADD COLUMN IF NOT EXISTS term_count integer NOT NULL DEFAULT 0;
        """
    )
    op.execute(
        "ALTER TABLE company_documents "
        "DROP CONSTRAINT IF EXISTS ck_company_documents_type;"
    )
    op.execute(
        """
        ALTER TABLE company_documents ADD CONSTRAINT ck_company_documents_type
            CHECK (document_type IN
                ('about_company','registration','iso','turnover',
                 'completion_certificate','pan','gst','other'));
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_document_pages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_document_version_id uuid NOT NULL
                REFERENCES company_document_versions(id) ON DELETE CASCADE,
            page_index integer NOT NULL,
            printed_label text,
            text text NOT NULL DEFAULT '',
            char_count integer NOT NULL DEFAULT 0,
            is_ocr boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_company_document_pages_version_page
                UNIQUE (company_document_version_id, page_index)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_document_terms (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
            company_document_id uuid NOT NULL
                REFERENCES company_documents(id) ON DELETE CASCADE,
            term text NOT NULL,
            normalized text NOT NULL,
            page_index integer NOT NULL,
            printed_label text,
            excerpt text NOT NULL,
            source text NOT NULL DEFAULT 'text_rule',
            rules_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_company_document_terms_document_term
                UNIQUE (company_document_id, normalized)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_document_terms_company_id "
        "ON company_document_terms (company_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_document_terms_company_normalized "
        "ON company_document_terms (company_id, normalized);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_document_pages_version_id "
        "ON company_document_pages (company_document_version_id);"
    )

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


def downgrade() -> None:
    op.execute(
        "DELETE FROM jobs WHERE kind = 'extract_company_document';"
    )
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN
            ('scrape_portal','sweep_portal','classify_corpus',
             'fetch_documents','refresh_tender',
             'extract_requirements','snapshot_tender',
             'revalidate_citations','send_reminders'));
        """
    )
    op.execute("DROP TABLE IF EXISTS company_document_terms;")
    op.execute("DROP TABLE IF EXISTS company_document_pages;")
    op.execute(
        """
        UPDATE company_documents SET document_type = 'other'
        WHERE document_type = 'about_company';
        """
    )
    op.execute(
        "ALTER TABLE company_documents "
        "DROP CONSTRAINT IF EXISTS ck_company_documents_type;"
    )
    op.execute(
        """
        ALTER TABLE company_documents ADD CONSTRAINT ck_company_documents_type
            CHECK (document_type IN
                ('registration','iso','turnover','completion_certificate',
                 'pan','gst','other'));
        """
    )
    op.execute(
        """
        ALTER TABLE company_documents
            DROP COLUMN IF EXISTS extraction_state,
            DROP COLUMN IF EXISTS extraction_error,
            DROP COLUMN IF EXISTS extracted_at,
            DROP COLUMN IF EXISTS term_count;
        """
    )
