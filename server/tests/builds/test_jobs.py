# ruff: noqa: E501
"""Background work: queueing, progress over NOTIFY, stopping, one build at a
time, the read-time reconcile and stalled-job recovery. Ported from
`opennotebook_build/src/job.rs` and the lifecycle tests of
`session_impl.rs`. Plan §9: one prep at a time; reconcile at read time."""

import asyncio
import contextlib
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import jobs
from opennotebook.build import pipeline
from opennotebook.db.models import Job
from opennotebook.db.session import engine, sessionmaker
from opennotebook.jobs import Progress, tasks, worker
from opennotebook.jobs.app import app, conninfo
from tests.builds.conftest import drain
from tests.builds.fake import install
from tests.model import add_note


async def _collection(client: AsyncClient) -> str:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    await add_note(client, cid, "Coral reefs are built by polyps over centuries. " * 10)
    return cid


async def _row(sql: str, **params: Any) -> Any:
    async with engine().begin() as c:
        return (await c.execute(text(sql), params)).mappings().first()


async def test_a_build_is_queued_with_its_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, tmp_path)
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["state"] == "preparing" and out["kind"] == "slides"
    assert out["title"] == "Editorial slides"
    job = await _row("SELECT * FROM jobs WHERE session_id = :s", s=out["id"])
    assert job is not None
    assert (job["status"], job["kind"], job["steps_total"]) == ("queued", "prep", 5)
    queued = await _row(
        "SELECT queue_name, task_name, lock, status::text AS status, args FROM procrastinate_jobs "
        "WHERE id = :id",
        id=job["procrastinate_job_id"],
    )
    assert queued is not None
    assert (queued["queue_name"], queued["task_name"], queued["lock"]) == ("prep", "prep", "prep")
    assert queued["status"] == "todo"
    assert queued["args"]["session_id"] == out["id"]
    assert queued["args"]["slide_count"] == 5


async def test_progress_is_announced_on_the_one_row(client: AsyncClient) -> None:
    owner = (await client.get("/api/me")).json()["id"]
    async with sessionmaker()() as s, s.begin():
        job = await jobs.create(s, uuid.UUID(owner), "prep", steps_total=4)
    async with await psycopg.AsyncConnection.connect(conninfo(), autocommit=True) as conn:
        await conn.execute(f"LISTEN {jobs.CHANNEL}")
        p = Progress(job.id, 4)
        await p.start()
        await p.phase("ingest")
        await p.phase_done("ingest")
        heard: list[str] = []

        async def listen() -> None:
            async for n in conn.notifies():
                heard.append(n.payload)
                if len(heard) == 3:
                    return

        await asyncio.wait_for(listen(), 5)
    assert heard == [str(job.id)] * 3, "the payload is the job's id and nothing else"
    row = await _row(
        "SELECT step, steps_done, steps_total, status FROM jobs WHERE id = :id", id=job.id
    )
    assert row is not None
    assert (row["step"], row["steps_done"], row["steps_total"], row["status"]) == (
        "ingest",
        1,
        4,
        "running",
    )


async def test_a_stop_holds_and_progress_for_no_row_is_an_error(client: AsyncClient) -> None:
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    cid = uuid.UUID(await _collection(client))
    async with engine().begin() as c:
        sid = (
            await c.execute(
                text(
                    "INSERT INTO sessions (owner_id, collection_id, kind) VALUES (:o, :c, 'slides') "
                    "RETURNING id"
                ),
                {"o": owner, "c": cid},
            )
        ).scalar_one()
    async with sessionmaker()() as s, s.begin():
        job = await jobs.create(s, owner, "prep", collection_id=cid, session_id=sid, steps_total=4)
    p = Progress(job.id, 4)
    await p.start()
    async with sessionmaker()() as s, s.begin():
        assert await jobs.stop(s, sid) == [job.id]
    # Progress from work that has not seen the stop yet must not undo it.
    await p.phase("script")
    assert await jobs.status_of(job.id) == "cancelled"
    async with sessionmaker()() as s, s.begin():
        assert await jobs.stop(s, sid) == [], "stopping again finds nothing live"
    with pytest.raises(jobs.NoJob):
        await Progress(uuid.uuid4(), 3).start()


async def test_one_build_runs_at_a_time(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, tmp_path)
    running, most = 0, 0
    real = pipeline.run

    async def counted(spec: pipeline.PrepSpec) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        try:
            await asyncio.sleep(0.2)
            await real(spec)
        finally:
            running -= 1

    monkeypatch.setattr(pipeline, "run", counted)
    cid = await _collection(client)
    sids = [
        (
            await client.post(
                f"/api/collections/{cid}/outputs", json={"kind": "audio", "audio_format": "brief"}
            )
        ).json()["id"]
        for _ in range(2)
    ]
    await drain("prep", concurrency=2)
    assert most == 1, "the prep lock lets one build run at a time"
    for sid in sids:
        assert (await client.get(f"/api/sessions/{sid}")).json()["state"] == "ready"


async def test_a_stop_reaches_the_running_build_as_cancellation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    studio, _ = install(monkeypatch, tmp_path)
    studio.hold["outline"] = asyncio.Event()
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    job = await _row("SELECT id, procrastinate_job_id FROM jobs WHERE session_id = :s", s=sid)
    assert job is not None

    async def work() -> None:
        async with app.open_async():
            await app.run_worker_async(
                queues=["prep"], install_signal_handlers=False, abort_job_polling_interval=0.1
            )

    running = asyncio.create_task(work())
    try:
        for _ in range(200):
            if "outline" in studio.asked:
                break
            await asyncio.sleep(0.05)
        assert "outline" in studio.asked, "the build reached the outline"
        async with sessionmaker()() as s, s.begin():
            await jobs.stop(s, uuid.UUID(sid))
        status = None
        for _ in range(100):
            q = await _row(
                "SELECT status::text AS status FROM procrastinate_jobs WHERE id = :id",
                id=job["procrastinate_job_id"],
            )
            status = q["status"] if q else None
            if status == "aborted":
                break
            await asyncio.sleep(0.05)
        assert status == "aborted"
    finally:
        studio.hold["outline"].set()
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
    assert await jobs.status_of(job["id"]) == "cancelled"
    # The row whose build stopped says so when it is read.
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["state"] == "failed"
    assert got["failure"] == "This build was stopped before it finished. Start it again to make it."


async def test_a_preparing_row_whose_job_died_is_failed_when_read(client: AsyncClient) -> None:
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    cid = uuid.UUID(await _collection(client))

    async def output(job_status: str | None, queued: str | None) -> str:
        async with sessionmaker()() as s, s.begin():
            sid = (
                await s.execute(
                    text(
                        "INSERT INTO sessions (owner_id, collection_id, kind, title) "
                        "VALUES (:o, :c, 'slides', 't') RETURNING id"
                    ),
                    {"o": owner, "c": cid},
                )
            ).scalar_one()
            if job_status is not None:
                job = await jobs.create(s, owner, "prep", collection_id=cid, session_id=sid)
                job.status = job_status
                if queued is not None:
                    await jobs.defer(s, job, "prep", {}, queue="prep")
                    await s.execute(
                        text(
                            "UPDATE procrastinate_jobs SET status = CAST(:q AS procrastinate_job_status) "
                            "WHERE id = :id"
                        ),
                        {"q": queued, "id": job.procrastinate_job_id},
                    )
        return str(sid)

    # No job at all: nothing is doing the work, and nothing ever will.
    orphan = await output(None, None)
    # Waiting its turn, or running: left alone.
    waiting = await output("queued", "todo")
    running = await output("running", "doing")
    # The queue says the work ended without our row hearing of it.
    died = await output("running", "failed")
    gone = await output("queued", None)

    listed = {o["id"]: o for o in (await client.get("/api/sessions")).json()}
    assert listed[orphan]["state"] == "failed"
    assert listed[orphan]["failure"].endswith("Try again.")
    assert listed[waiting]["state"] == "preparing"
    assert listed[running]["state"] == "preparing"
    assert listed[died]["state"] == "failed"
    assert listed[gone]["state"] == "preparing", "queued with no queue entry yet is still ours"
    # Written, not just shown: the row says failed from now on.
    row = await _row("SELECT state FROM sessions WHERE id = :id", id=orphan)
    assert row is not None and row["state"] == "failed"


async def test_work_a_dead_worker_left_is_run_again_once_then_failed(client: AsyncClient) -> None:
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    async with sessionmaker()() as s, s.begin():
        job = await jobs.create(s, owner, "research")
        await jobs.defer(s, job, tasks.RESEARCH_TASK, {}, queue="work")
        job.status = "running"
        pid = job.procrastinate_job_id
    # A worker took it and died: `doing`, with no worker behind it.
    await _row(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL WHERE id = :id RETURNING id",
        id=pid,
    )
    async with app.open_async():
        assert await worker.recover_stalled() == 1
        q = await _row(
            "SELECT status::text AS status FROM procrastinate_jobs WHERE id = :id", id=pid
        )
        assert q is not None and q["status"] == "todo"
        assert await jobs.status_of(job.id) == "queued"
        # It dies again: failed this time, with a sentence.
        await _row(
            "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL, attempts = 2 "
            "WHERE id = :id RETURNING id",
            id=pid,
        )
        assert await worker.recover_stalled() == 1
    q = await _row("SELECT status::text AS status FROM procrastinate_jobs WHERE id = :id", id=pid)
    assert q is not None and q["status"] == "failed"
    row = await _row("SELECT status, error FROM jobs WHERE id = :id", id=job.id)
    assert row is not None and row["status"] == "failed"
    assert row["error"] == worker.INTERRUPTED


async def test_arguments_that_do_not_decode_fail_the_job(client: AsyncClient) -> None:
    # The failure this guards: a spec that silently became empty would
    # prepare an empty output and call it success.
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    async with sessionmaker()() as s, s.begin():
        job = await jobs.create(s, owner, "prep")
    await tasks.prep(job_id=str(job.id), slide_count="lots")
    async with sessionmaker()() as s:
        row = await s.get(Job, job.id)
        assert row is not None
        assert row.status == "failed"
        assert "did not decode" in (row.error or "")
