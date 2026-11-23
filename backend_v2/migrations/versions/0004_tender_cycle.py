"""Record a tender's bidding cycle state.

A recheck can find the portal stating that the current bidding cycle is
terminated. That is not a field change in the notice: the tender is over and
the interface must say so instead of showing the old notice's details. The
state is kept on the tender with the portal's own sentence as evidence, so a
terminated cycle is never inferred from a missing notice or a passed deadline.

Revision ID: 0004_tender_cycle
Revises: 0003_refresh_job_kind
Create Date: 2026-11-03
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_tender_cycle"
down_revision: Union[str, None] = "0003_refresh_job_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS cycle_state TEXT "
        "NOT NULL DEFAULT 'active';"
    )
    op.execute(
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS termination_excerpt TEXT;"
    )
    op.execute(
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS "
        "termination_observed_at TIMESTAMPTZ;"
    )
    op.execute(
        "ALTER TABLE tenders DROP CONSTRAINT IF EXISTS ck_tenders_cycle_state;"
    )
    op.execute(
        "ALTER TABLE tenders ADD CONSTRAINT ck_tenders_cycle_state "
        "CHECK (cycle_state IN ('active','terminated'));"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenders DROP CONSTRAINT IF EXISTS ck_tenders_cycle_state;")
    op.execute("ALTER TABLE tenders DROP COLUMN IF EXISTS termination_observed_at;")
    op.execute("ALTER TABLE tenders DROP COLUMN IF EXISTS termination_excerpt;")
    op.execute("ALTER TABLE tenders DROP COLUMN IF EXISTS cycle_state;")
