"""Allow the sweep and the corpus-classification jobs.

The corpus-first layer adds two durable jobs that did not exist when
``ck_jobs_kind`` was last written:

* ``sweep_portal`` - the nightly listing sweep. It holds the browser lane while
  it reads the directory and the list pages, commits one page at a time and
  stops at a page boundary when cancelled.
* ``classify_corpus`` - the labelling and verdict pass. It is global (no
  company), so it runs on the default lane and is queued by a finished sweep.

Without this migration ``JobService.enqueue`` refuses neither kind, but the
database does: the insert fails a check constraint, so the driver gets a 500
and the sweep can never start.

Revision ID: 0006_sweep_job_kind
Revises: 0005_corpus_intelligence
Create Date: 2026-11-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_sweep_job_kind"
down_revision: Union[str, None] = "0005_corpus_intelligence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    # A sweep or classification row cannot survive the narrower constraint, so
    # the rows are removed first. They are background bookkeeping, not the
    # corpus: the tenders they read stay.
    op.execute(
        "DELETE FROM runs WHERE job_id IN "
        "(SELECT id FROM jobs WHERE kind IN ('sweep_portal','classify_corpus'));"
    )
    op.execute(
        "DELETE FROM jobs WHERE kind IN ('sweep_portal','classify_corpus');"
    )
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN
            ('scrape_portal','fetch_documents','refresh_tender',
             'extract_requirements','snapshot_tender',
             'revalidate_citations','send_reminders'));
        """
    )
