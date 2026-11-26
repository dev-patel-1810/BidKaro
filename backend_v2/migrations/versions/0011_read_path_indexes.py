"""Indexes for the tender read path

Revision ID: 0011_read_path_indexes
Revises: 0010_company_brief
Create Date: 2026-11-26

The tender list narrows the corpus with ``ILIKE '%term%'`` fragments over the
work fields and then orders the open rows. Without a trigram index those
fragments scan every row, so a growing corpus makes the first page slower every
night. The GIN indexes below make the narrowing a bitmap lookup; the partial
btree indexes make the default order and the closing-date filter cheap.

The extension is created defensively: a database whose role cannot install
``pg_trgm`` still migrates, and only the trigram indexes are skipped.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0011_read_path_indexes"
down_revision: Union[str, None] = "0010_company_brief"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'pg_trgm is not available; trigram indexes skipped';
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
                CREATE INDEX IF NOT EXISTS ix_tenders_work_title_trgm
                    ON tenders USING gin (work_title gin_trgm_ops)
                    WHERE deleted_at IS NULL AND merged_into_tender_id IS NULL;
                CREATE INDEX IF NOT EXISTS ix_tenders_work_type_trgm
                    ON tenders USING gin (work_type gin_trgm_ops)
                    WHERE deleted_at IS NULL AND merged_into_tender_id IS NULL;
                CREATE INDEX IF NOT EXISTS ix_tenders_location_trgm
                    ON tenders USING gin (location gin_trgm_ops)
                    WHERE deleted_at IS NULL AND merged_into_tender_id IS NULL;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenders_open_created
            ON tenders (created_at DESC, id DESC)
            WHERE deleted_at IS NULL AND merged_into_tender_id IS NULL;
        CREATE INDEX IF NOT EXISTS ix_tenders_open_closing
            ON tenders (closing_at)
            WHERE deleted_at IS NULL AND merged_into_tender_id IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_tenders_open_closing;
        DROP INDEX IF EXISTS ix_tenders_open_created;
        DROP INDEX IF EXISTS ix_tenders_location_trgm;
        DROP INDEX IF EXISTS ix_tenders_work_type_trgm;
        DROP INDEX IF EXISTS ix_tenders_work_title_trgm;
        """
    )
