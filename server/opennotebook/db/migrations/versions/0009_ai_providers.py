"""AI providers connected from the web app, with their keys encrypted. Also
joins the two heads, 0007 and 0008, into one.

Revision ID: 0009
Revises: 0007, 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = ("0007", "0008")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), server_default="", nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("key_encrypted", sa.Text(), server_default="", nullable=False),
        sa.Column("last_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_providers")),
    )


def downgrade() -> None:
    op.drop_table("ai_providers")
