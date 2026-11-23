"""Sharing a collection in Discover, and where a reused one came from.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("include_sources", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("outputs", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("reuses", sa.Integer(), server_default="0", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_shares_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_shares_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shares")),
        sa.UniqueConstraint("collection_id", name=op.f("uq_shares_collection_id")),
    )
    op.create_index(op.f("ix_shares_owner_id"), "shares", ["owner_id"], unique=False)
    op.create_index("ix_shares_updated", "shares", ["updated_at"], unique=False)
    op.add_column("collections", sa.Column("reused_from", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("collections", "reused_from")
    op.drop_index("ix_shares_updated", table_name="shares")
    op.drop_index(op.f("ix_shares_owner_id"), table_name="shares")
    op.drop_table("shares")
