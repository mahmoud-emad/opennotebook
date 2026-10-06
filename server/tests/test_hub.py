"""The event hub wakes only the streams a change is about, remembers a bounded
number of jobs, and a stream reads again only what the change can have
touched."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.api.collections import events_of
from opennotebook.db.session import engine
from opennotebook.domain.sessions_events import stream
from opennotebook.jobs import CHANNEL, COLLECTION_CHANNEL
from opennotebook.jobs import events as ev
from opennotebook.jobs.events import JOB, SESSION, SESSION_CHANNEL, Hub, Wake, hub
from tests.queries import recorded


async def _sql(sql: str, **kw: Any) -> Any:
    async with engine().begin() as c:
        r = await c.execute(text(sql), kw)
        return r.scalar() if r.returns_rows else None


async def _owner(client: AsyncClient) -> str:
    return (await client.get("/api/me")).json()["id"]


async def _making(client: AsyncClient) -> tuple[str, str, str, str]:
    """A collection with a deck being made and its job: (owner, cid, sid, job)."""
    owner = await _owner(client)
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    sid = str(
        await _sql(
            "INSERT INTO sessions (owner_id, collection_id, kind, title, state, slides)"
            " VALUES (:o, :c, 'slides', 'Deck', 'preparing', CAST(:s AS jsonb)) RETURNING id",
            o=owner,
            c=cid,
            s=json.dumps([{"title": "One", "lines": [{"line_id": "l0", "speaker_id": "host"}]}]),
        )
    )
    job = str(
        await _sql(
            "INSERT INTO jobs (owner_id, kind, status, session_id, collection_id, step,"
            " steps_done, steps_total) VALUES (:o, 'prep', 'running', :s, :c, 'Writing', 1, 5)"
            " RETURNING id",
            o=owner,
            s=sid,
            c=cid,
        )
    )
    return owner, cid, sid, job


def _woke(w: Wake) -> set[str]:
    return w.clear()


async def test_a_job_no_stream_has_seen_wakes_only_what_it_is_about(
    client: AsyncClient,
) -> None:
    _, cid, sid, job = await _making(client)
    other = uuid.uuid4()
    h = Hub()
    try:
        async with (
            h.subscribe(uuid.UUID(cid)) as mine,
            h.subscribe(other) as theirs,
            h.subscribe(uuid.UUID(sid)) as output,
        ):
            h._on(CHANNEL, job)  # pyright: ignore[reportPrivateUsage]
            async with asyncio.timeout(5):
                await mine.wait()
                await output.wait()
            assert _woke(mine) == {JOB} and _woke(output) == {JOB}
            assert _woke(theirs) == set(), "a stream it is not about sleeps on"
            # Remembered: the next report needs no lookup.
            with recorded() as q:
                h._on(CHANNEL, job)  # pyright: ignore[reportPrivateUsage]
            assert len(q) == 0 and _woke(mine) == {JOB} and _woke(output) == {JOB}
            # A job that is not there wakes nobody.
            h._on(CHANNEL, str(uuid.uuid4()))  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0.2)
            assert _woke(mine) == set() and _woke(theirs) == set()
            # A change to a collection or a playhead says which it was.
            h._on(COLLECTION_CHANNEL, cid)  # pyright: ignore[reportPrivateUsage]
            h._on(SESSION_CHANNEL, sid)  # pyright: ignore[reportPrivateUsage]
            assert _woke(mine) == {"collection"} and _woke(output) == {SESSION}
    finally:
        await h.close()


def test_the_jobs_it_remembers_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev, "JOBS_MAX", 3)
    h = Hub()
    jobs = [uuid.uuid4() for _ in range(5)]
    key = uuid.uuid4()
    for j in jobs:
        h.follow_job(j, key)
    remembered = list(h._jobs)  # pyright: ignore[reportPrivateUsage]
    assert remembered == jobs[2:], "the oldest are forgotten first"


async def _next(it: AsyncIterator[Any]) -> tuple[str, Any]:
    while True:
        e = await asyncio.wait_for(anext(it), 10)
        if e is not None:
            return e


async def test_a_jobs_report_reads_only_the_job_again(client: AsyncClient) -> None:
    owner, cid, _, job = await _making(client)
    it = events_of(uuid.UUID(owner), uuid.UUID(cid))
    try:
        first = [await _next(it) for _ in range(6)]
        assert [n for n, _ in first] == [
            "collection",
            "outputs",
            "sources",
            "mindmaps",
            "notes",
            "progress",
        ]
        assert first[5][1]["steps_done"] == 1
        await _sql("UPDATE jobs SET steps_done = 2, step = 'Voicing' WHERE id = :j", j=job)
        with recorded() as q:
            hub.wake(uuid.UUID(cid), JOB)
            name, data = await _next(it)
        assert (name, data["steps_done"], data["step"]) == ("progress", 2, "Voicing")
        assert q and all("FROM jobs" in s for s in q), q
    finally:
        await it.aclose()
        await hub.close()


async def test_a_playhead_that_moves_reads_only_the_playhead(client: AsyncClient) -> None:
    owner, _, sid, _ = await _making(client)
    it = stream(uuid.UUID(owner), uuid.UUID(sid))
    try:
        assert [(await _next(it))[0] for _ in range(2)] == ["session.state", "prep.progress"]
        await _sql(
            "INSERT INTO playback (session_id, owner_id, state, line_id, offset_ms)"
            " VALUES (:s, :o, 'playing', 'l0', 10)",
            s=sid,
            o=owner,
        )
        seen: list[str] = []
        with recorded() as q:
            hub.wake(uuid.UUID(sid), SESSION)
            while not seen or seen[-1] != "playhead":
                seen.append((await _next(it))[0])
        assert "line.start" in seen
        assert q and all("FROM playback" in s for s in q), q
    finally:
        await it.aclose()
        await hub.close()
