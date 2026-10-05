"""Passage offsets and the embedding model, as the Rust memory store kept them.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("start", sa.Integer(), server_default="0", nullable=False))
    op.add_column("chunks", sa.Column("end", sa.Integer(), server_default="0", nullable=False))
    op.add_column("chunks", sa.Column("embed_model", sa.Text(), nullable=True))
    op.add_column("qa_pairs", sa.Column("dimension", sa.Text(), server_default="", nullable=False))
    op.add_column("qa_pairs", sa.Column("embed_model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("qa_pairs", "embed_model")
    op.drop_column("qa_pairs", "dimension")
    op.drop_column("chunks", "embed_model")
    op.drop_column("chunks", "end")
    op.drop_column("chunks", "start")
