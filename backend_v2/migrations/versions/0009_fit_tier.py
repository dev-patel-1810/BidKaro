"""Fit tier

Revision ID: 0009_fit_tier
Revises: 0008_company_understanding
Create Date: 2026-11-20

The fit ladder is primary; the score is a band inside the rung. A cached
verdict stores both so the list can sort by tier without recomputing every row.
Rows written before this migration default to tier 7; their ``rules_version``
is older, so the first read recomputes them.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0009_fit_tier"
down_revision: Union[str, None] = "0008_company_understanding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE company_tender_verdicts
            ADD COLUMN IF NOT EXISTS tier integer NOT NULL DEFAULT 7;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE company_tender_verdicts
            DROP COLUMN IF EXISTS tier;
        """
    )
