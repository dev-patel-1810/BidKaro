"""Add the pending signup payload to OTP challenges.

The 2026-10-26 auth decision says a pending signup is an `otp_codes` row and
the user/company are created only after verification.  The row therefore has
to carry the hashed password and company name until the code is accepted.

Revision ID: 0002_otp_payload
Revises: 0001_initial
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_otp_payload"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE otp_codes ADD COLUMN payload jsonb NOT NULL DEFAULT '{}'::jsonb;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE otp_codes DROP COLUMN payload;")
