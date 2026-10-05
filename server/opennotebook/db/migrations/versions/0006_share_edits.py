"""Whether a share lets people edit their copies, and which copies are
read-only.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shares",
        sa.Column("allow_edits", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "collections",
        sa.Column("read_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("collections", "read_only")
    op.drop_column("shares", "allow_edits")
