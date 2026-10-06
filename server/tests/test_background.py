"""Mind maps and study notes are made by the worker: asking answers at once
with the job and a row being made; the row is drawn, or removed with its job
saying why. And the queue tidies what finished long ago, never the spend
ledger."""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.api.collections import events_of
from opennotebook.db.session import engine
from opennotebook.jobs import background
from opennotebook.jobs.app import app
from opennotebook.jobs.events import hub
from tests.model import add_note, fails, install, run_work, says
from tests.test_mindmap import OUTLINE, REEFS


async def _sql(sql: str, **kw: Any) -> Any:
    async with engine().begin() as c:
        r = await c.execute(text(sql), kw)
        return r.scalar() if r.returns_rows else None


async def _collection(client: AsyncClient) -> str:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    await add_note(client, cid, REEFS, title="Reefs")
    return cid


async def test_a_map_is_listed_as_being_made_until_the_worker_draws_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    model.answers(says(OUTLINE))
    r = await client.post(f"/api/collections/{cid}/mindmaps", json={})
    assert r.status_code == 202, r.text
    job, row = r.json()["job"], r.json()["mindmap"]
    assert job["status"] == "queued" and job["kind"] == "mindmap"
    assert (row["state"], row["job_id"], row["node_count"]) == ("making", job["id"], 0)
    # Nothing was asked of the model yet: the request did not wait on it.
    assert model.bodies == []

    listed = (await client.get(f"/api/collections/{cid}/mindmaps")).json()
    assert [(m["id"], m["state"]) for m in listed] == [(row["id"], "making")]
    # A share cannot include it while it is being made.
    state = (await client.get(f"/api/collections/{cid}/share")).json()
    assert all(not i["key"].startswith("mindmap:") for i in state["items"])

    await run_work()
    done = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert (done["status"], done["steps_done"], done["steps_total"]) == ("done", 1, 1)
    m = (await client.get(f"/api/collections/{cid}/mindmaps/{row['id']}")).json()
    assert m["state"] == "ready" and m["root"]["name"] == "Coral reefs"
    state = (await client.get(f"/api/collections/{cid}/share")).json()
    assert f"mindmap:{row['id']}" in [i["key"] for i in state["items"]]


async def test_the_collection_stream_follows_a_map_and_says_how_its_making_ended(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_collections import _next  # pyright: ignore[reportPrivateUsage]

    model = install(monkeypatch)
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    cid = await _collection(client)
    ev = events_of(owner, uuid.UUID(cid))

    def anything(_: Any) -> bool:
        return True

    async def until(name: str, ok: Callable[[Any], bool] = anything) -> Any:
        while True:
            got, data = await _next(ev)
            if got == name and ok(data):
                return data

    try:
        assert await until("mindmaps") == []
        model.answers(says(OUTLINE))
        r = await client.post(f"/api/collections/{cid}/mindmaps", json={})
        job, row = r.json()["job"], r.json()["mindmap"]
        listed = await until("mindmaps", lambda d: d != [])
        assert [(m["id"], m["state"], m["job_id"]) for m in listed] == [
            (row["id"], "making", job["id"])
        ]
        await run_work()
        # The list first, drawn; then the job's end, with nothing wrong.
        listed = await until("mindmaps", lambda d: d[0]["state"] == "ready")
        assert [m["id"] for m in listed] == [row["id"]]
        assert await until("ended") == {"job_id": job["id"], "error": None}

        model.answers(fails(401, "No auth credentials found"))
        r = await client.post(f"/api/collections/{cid}/notes", json={})
        job = r.json()["job"]
        await until("notes", lambda d: d != [])
        await run_work()
        seen: dict[str, Any] = {}
        while "ended" not in seen:
            got, data = await _next(ev)
            seen[got] = data
        assert seen["ended"]["job_id"] == job["id"]
        assert isinstance(seen["ended"]["error"], str) and seen["ended"]["error"].endswith(".")
        # The notes that were not made left the list before their job ended.
        assert seen["notes"] == []
    finally:
        await ev.aclose()
        await hub.close()


async def test_work_that_ended_without_a_reason_still_says_one(client: AsyncClient) -> None:
    cid = await _collection(client)
    job = (await client.post(f"/api/collections/{cid}/notes", json={})).json()["job"]
    await _sql("UPDATE jobs SET status = 'failed', error = NULL WHERE id = :j", j=job["id"])
    got = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert got["status"] == "failed"
    assert got["error"] == "This was stopped before it finished. Start it again to make it."


async def test_notes_being_written_are_stopped_when_deleted(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    assert r.status_code == 202, r.text
    job, row = r.json()["job"], r.json()["notes"]
    assert row["state"] == "making" and (row["ideas"], row["headings"]) == (0, [])
    assert (await client.delete(f"/api/collections/{cid}/notes/{row['id']}")).status_code == 204
    stopped = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert stopped["status"] == "cancelled"
    assert stopped["error"] == "This was stopped before it finished. Start it again to make it."
    # The worker finds nothing waiting for it.
    await run_work()
    assert (await client.get(f"/api/collections/{cid}/notes")).json() == []


async def test_a_row_whose_making_died_is_removed_when_listed(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/mindmaps", json={})
    job, row = r.json()["job"], r.json()["mindmap"]
    # The worker was killed: the queue's record of the job is gone with it.
    await _sql("DELETE FROM procrastinate_jobs")
    assert (await client.get(f"/api/collections/{cid}/mindmaps")).json() == []
    assert await _sql("SELECT count(*) FROM mindmaps WHERE id = :i", i=row["id"]) == 0
    assert job["id"]


async def test_a_job_with_arguments_it_cannot_read_fails_in_words(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    job = r.json()["job"]
    await background.make_notes(job_id=job["id"], owner_id="nobody")  # pyright: ignore[reportCallIssue]
    failed = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert failed["status"] == "failed"
    assert failed["error"].startswith("This work reached the worker in a form it could not read")


async def test_a_read_only_copy_and_a_missing_source_are_refused_before_anything_starts(
    client: AsyncClient,
) -> None:
    cid = await _collection(client)
    await _sql("UPDATE collections SET read_only = true WHERE id = :c", c=cid)
    for kind in ("mindmaps", "notes"):
        r = await client.post(f"/api/collections/{cid}/{kind}", json={})
        assert r.status_code == 403 and "read-only copy" in r.json()["detail"]
    await _sql("UPDATE collections SET read_only = false WHERE id = :c", c=cid)
    r = await client.post(f"/api/collections/{cid}/mindmaps", json={"sources": ["nope.md"]})
    assert r.status_code == 422 and "“nope.md” is not a source" in r.json()["detail"]
    assert await _sql("SELECT count(*) FROM jobs WHERE kind IN ('mindmap', 'notes')") == 0


# ── tidying ───────────────────────────────────────────────────────────────────


def test_the_tidy_runs_every_hour() -> None:
    tasks = {p.task.name for p in app.periodic_registry.periodic_tasks.values()}
    assert background.TIDY_TASK in tasks


async def test_old_finished_jobs_go_and_the_spend_ledger_stays(client: AsyncClient) -> None:
    me = (await client.get("/api/me")).json()["id"]
    cid = await _collection(client)
    old = datetime.now(UTC) - timedelta(days=background.JOBS_KEEP_DAYS + 1)
    recent = datetime.now(UTC) - timedelta(days=1)

    async def session(state: str) -> str:
        return str(
            await _sql(
                "INSERT INTO sessions (owner_id, collection_id, kind, title, state)"
                " VALUES (:o, :c, 'slides', 'Deck', :s) RETURNING id",
                o=me,
                c=cid,
                s=state,
            )
        )

    async def job(status: str, finished: datetime | None, sid: str | None = None) -> str:
        return str(
            await _sql(
                "INSERT INTO jobs (owner_id, kind, status, collection_id, session_id, finished_at)"
                " VALUES (:o, 'prep', :st, :c, :s, :f) RETURNING id",
                o=me,
                st=status,
                c=cid,
                s=sid,
                f=finished,
            )
        )

    gone = [
        await job("done", old),
        await job("cancelled", old),
        await job("done", old, await session("ready")),
    ]
    kept = [
        await job("done", recent),
        await job("running", None),
        # The newest job of a failed output still says why it failed.
        await job("failed", old, await session("failed")),
    ]
    await _sql(
        "INSERT INTO usage_events (owner_id, kind, model, priced_by, created_at, job_id)"
        " VALUES (:o, 'script', 'm', 'unpriced', :at, CAST(:j AS uuid))",
        o=me,
        at=old,
        j=gone[0],
    )
    assert await background.prune_jobs() == len(gone)
    left = await _sql("SELECT json_agg(id) FROM jobs")
    assert sorted(left if isinstance(left, list) else json.loads(left)) == sorted(kept)
    assert await _sql("SELECT count(*) FROM usage_events") == 1
    # The whole tidy, as the worker runs it, removes nothing more.
    async with app.open_async():
        await background.tidy(timestamp=0)
    assert await _sql("SELECT count(*) FROM jobs") == len(kept)
