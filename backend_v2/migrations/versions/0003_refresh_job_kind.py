"""Allow a per-tender source recheck job.

Stage K adds ``refresh_tender`` as a durable browser-lane job, so the job kind
check constraint has to accept it. The old value is replaced rather than
widened in place because the constraint is a single named check.

Revision ID: 0003_refresh_job_kind
Revises: 0002_otp_payload
Create Date: 2026-10-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_refresh_job_kind"
down_revision: Union[str, None] = "0002_otp_payload"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN
            ('scrape_portal','fetch_documents','refresh_tender',
             'extract_requirements','snapshot_tender',
             'revalidate_citations','send_reminders'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_kind;")
    op.execute(
        """
        ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN
            ('scrape_portal','fetch_documents','extract_requirements',
             'snapshot_tender','revalidate_citations','send_reminders'));
        """
    )
