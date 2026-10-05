"""Indexes for the queries the studio runs most: an output's newest job, a
queue job's own row, finished jobs to prune, a person's outputs newest
first, the outputs still being made, Discover's "most reused" order, and a
collection's sources in the order they were added. And mind maps and study
notes made in the background: each row says whether it is still being made
(`state`) and by which job (`job_id`).

`ix_jobs_session_created` answers everything `ix_jobs_session_id` did, so
that one goes.

Revision ID: 0008
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_jobs_session_created", "jobs", ["session_id", "created_at"])
    op.drop_index("ix_jobs_session_id", table_name="jobs")
    op.create_index(
        "ix_jobs_procrastinate_job_id",
        "jobs",
        ["procrastinate_job_id"],
        postgresql_where=sa.text("procrastinate_job_id IS NOT NULL"),
    )
    op.create_index(
        "ix_jobs_finished",
        "jobs",
        ["finished_at"],
        postgresql_where=sa.text("finished_at IS NOT NULL"),
    )
    op.create_index("ix_sessions_owner_created", "sessions", ["owner_id", "created_at"])
    op.create_index(
        "ix_sessions_preparing",
        "sessions",
        ["collection_id"],
        postgresql_where=sa.text("state = 'preparing'"),
    )
    op.create_index("ix_shares_reuses", "shares", ["reuses", "updated_at", "id"])
    op.create_index("ix_sources_collection_created", "sources", ["collection_id", "created_at"])
    for table in MADE:
        op.add_column(table, sa.Column("state", sa.Text(), server_default="ready", nullable=False))
        op.add_column(table, sa.Column("job_id", sa.UUID(), nullable=True))
        op.create_check_constraint(op.f(f"ck_{table}_state"), table, "state IN ('making', 'ready')")


# The outputs made in one call, now in the background.
MADE = ("mindmaps", "study_notes")


def downgrade() -> None:
    for table in MADE:
        # A row still being made has nothing in it to keep.
        op.execute(f"DELETE FROM {table} WHERE state = 'making'")
        op.drop_constraint(op.f(f"ck_{table}_state"), table, type_="check")
        op.drop_column(table, "job_id")
        op.drop_column(table, "state")
    op.drop_index("ix_sources_collection_created", table_name="sources")
    op.drop_index("ix_shares_reuses", table_name="shares")
    op.drop_index("ix_sessions_preparing", table_name="sessions")
    op.drop_index("ix_sessions_owner_created", table_name="sessions")
    op.drop_index("ix_jobs_finished", table_name="jobs")
    op.drop_index("ix_jobs_procrastinate_job_id", table_name="jobs")
    op.create_index("ix_jobs_session_id", "jobs", ["session_id"])
    op.drop_index("ix_jobs_session_created", table_name="jobs")
