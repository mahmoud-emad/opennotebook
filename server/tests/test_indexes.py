"""The indexes the studio's common queries lean on are there, and the planner
can answer those queries from them rather than reading whole tables."""

from typing import Any

from sqlalchemy import text

from opennotebook.db.models import Base
from opennotebook.db.session import engine

WANTED = {
    "jobs": {"ix_jobs_session_created", "ix_jobs_procrastinate_job_id", "ix_jobs_finished"},
    "sessions": {"ix_sessions_owner_created", "ix_sessions_preparing"},
    "shares": {"ix_shares_reuses"},
    "sources": {"ix_sources_collection_created"},
}


async def _indexes(table: str) -> set[str]:
    async with engine().connect() as c:
        rows = await c.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
        )
        return {r[0] for r in rows}


async def test_the_migration_made_every_index_the_models_declare() -> None:
    for table, names in WANTED.items():
        assert names <= await _indexes(table), table
    # The one the newest-job index replaced is gone.
    assert "ix_jobs_session_id" not in await _indexes("jobs")
    declared = {i.name for t in Base.metadata.sorted_tables for i in t.indexes}
    assert set().union(*WANTED.values()) <= declared


async def _plan(sql: str, **params: Any) -> str:
    """The plan of `sql` with sequential scans priced out, so a test table of
    a few rows still shows which index the query can use."""
    async with engine().connect() as c:
        await c.execute(text("SET LOCAL enable_seqscan = off"))
        rows = await c.execute(text(f"EXPLAIN {sql}"), params)
        plan = "\n".join(r[0] for r in rows)
        await c.rollback()
    return plan


async def test_an_outputs_newest_job_is_read_from_its_index() -> None:
    plan = await _plan(
        "SELECT * FROM jobs WHERE session_id = :s ORDER BY created_at DESC LIMIT 1",
        s="00000000-0000-0000-0000-000000000000",
    )
    assert "ix_jobs_session_created" in plan
    assert "Sort" not in plan


async def test_a_queue_jobs_row_is_found_by_its_index() -> None:
    plan = await _plan("SELECT id FROM jobs WHERE procrastinate_job_id = 7")
    assert "ix_jobs_procrastinate_job_id" in plan


async def test_a_persons_outputs_are_listed_newest_first_from_an_index() -> None:
    plan = await _plan(
        "SELECT id FROM sessions WHERE owner_id = :o ORDER BY created_at DESC LIMIT 50",
        o="00000000-0000-0000-0000-000000000000",
    )
    assert "ix_sessions_owner_created" in plan
    assert "Sort" not in plan


async def test_outputs_being_made_are_counted_from_the_partial_index() -> None:
    plan = await _plan(
        "SELECT count(*) FROM sessions WHERE collection_id = :c AND state = 'preparing'",
        c="00000000-0000-0000-0000-000000000000",
    )
    assert "ix_sessions_preparing" in plan
