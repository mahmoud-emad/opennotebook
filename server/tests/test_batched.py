"""Reads that used to cost a query per row now cost the same however many
rows there are: the jobs of the outputs being made, the covers setting of
each sharer, and the copy a reuse makes."""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine
from tests.conftest import other_person
from tests.queries import recorded


async def _me(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    return (await client.get("/api/me", headers=headers)).json()["id"]


async def _making(owner: str, cid: str, n: int, status: str = "queued") -> None:
    """`n` outputs being made, each with a job of its own in `status`."""
    async with engine().begin() as c:
        for i in range(n):
            sid = (
                await c.execute(
                    text(
                        "INSERT INTO sessions (owner_id, collection_id, kind, title, state)"
                        " VALUES (:o, :c, 'slides', :t, 'preparing') RETURNING id"
                    ),
                    {"o": owner, "c": cid, "t": f"Deck {i}"},
                )
            ).scalar_one()
            await c.execute(
                text(
                    "INSERT INTO jobs (owner_id, kind, status, session_id, collection_id,"
                    " step, steps_done, steps_total) VALUES (:o, 'prep', :st, :s, :c,"
                    " 'Writing', 1, 5)"
                ),
                {"o": owner, "c": cid, "s": sid, "st": status},
            )


async def _failed(owner: str, cid: str, n: int) -> None:
    async with engine().begin() as c:
        for i in range(n):
            sid = (
                await c.execute(
                    text(
                        "INSERT INTO sessions (owner_id, collection_id, kind, title, state,"
                        " failure) VALUES (:o, :c, 'audio', :t, 'failed', 'It broke.')"
                        " RETURNING id"
                    ),
                    {"o": owner, "c": cid, "t": f"Talk {i}"},
                )
            ).scalar_one()
            await c.execute(
                text(
                    "INSERT INTO jobs (owner_id, kind, status, error, session_id, collection_id)"
                    " VALUES (:o, 'prep', 'failed', 'detail', :s, :c)"
                ),
                {"o": owner, "c": cid, "s": sid},
            )


async def _reading(client: AsyncClient, cid: str) -> tuple[int, dict[str, Any]]:
    with recorded() as q:
        r = await client.get(f"/api/collections/{cid}")
    assert r.status_code == 200, r.text
    return len(q), r.json()


async def test_a_collections_outputs_cost_the_same_however_many_are_being_made(
    client: AsyncClient,
) -> None:
    me = await _me(client)
    cid = (await client.post("/api/collections", json={"title": "Busy"})).json()["id"]
    await _making(me, cid, 1)
    await _failed(me, cid, 1)
    few, _ = await _reading(client, cid)
    await _making(me, cid, 5)
    await _failed(me, cid, 3)
    many, detail = await _reading(client, cid)
    assert many == few
    outputs = detail["outputs"]
    assert len(outputs) == 10
    # Queued with no worker running: each says why it waits.
    making = [o for o in outputs if o["state"] == "preparing"]
    assert len(making) == 6 and all(o["waiting"] for o in making)
    failed = [o for o in outputs if o["state"] == "failed"]
    assert all(o["failure_detail"] == "detail" for o in failed)


async def test_an_output_whose_job_died_is_still_marked_failed_in_a_list(
    client: AsyncClient,
) -> None:
    me = await _me(client)
    cid = (await client.post("/api/collections", json={"title": "Dead"})).json()["id"]
    await _making(me, cid, 2, status="cancelled")
    await _making(me, cid, 1, status="running")
    listed = (await client.get("/api/sessions")).json()
    states = sorted(o["state"] for o in listed)
    assert states == ["failed", "failed", "preparing"]
    dead = [o for o in listed if o["state"] == "failed"]
    assert all(o["failure"] and o["failure"].endswith("Start it again to make it.") for o in dead)


async def test_the_event_stream_sees_the_progress_of_every_output_being_made(
    client: AsyncClient,
) -> None:
    from opennotebook.api.collections import _read  # pyright: ignore[reportPrivateUsage]

    me = await _me(client)
    cid = (await client.post("/api/collections", json={"title": "Busy"})).json()["id"]
    await _making(me, cid, 3)
    now = await _read(uuid.UUID(me), uuid.UUID(cid))
    assert now is not None
    assert len(now["progress"]) == 3
    assert {p["steps_total"] for p in now["progress"].values()} == {5}


async def _shared(client: AsyncClient, sources: int, headers: dict[str, str]) -> str:
    cid = (await client.post("/api/collections", json={"title": "Copied"}, headers=headers)).json()[
        "id"
    ]
    for i in range(sources):
        r = await client.post(
            f"/api/collections/{cid}/sources",
            json={"kind": "text", "text": f"Source {i}: reefs are built by coral polyps slowly."},
            headers=headers,
        )
        assert r.status_code == 201, r.text
    r = await client.post(
        f"/api/collections/{cid}/shares", json={"include_sources": True}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _reusing(client: AsyncClient, share_id: str) -> tuple[int, str]:
    with recorded() as q:
        r = await client.post(f"/api/shares/{share_id}/reuse")
    assert r.status_code == 201, r.text
    return len(q), r.json()["id"]


async def test_a_reuse_copies_every_source_in_the_same_few_statements(
    client: AsyncClient,
) -> None:
    other = await other_person(client, "sharer@example.org")
    few, _ = await _reusing(client, await _shared(client, 1, other))
    many, copy = await _reusing(client, await _shared(client, 4, other))
    assert many == few
    # The copy holds every source with its passages, and they are searched
    # like the original's.
    sources = (await client.get(f"/api/collections/{copy}/sources")).json()
    assert len(sources) == 4
    async with engine().connect() as c:
        chunks = await c.scalar(
            text(
                "SELECT count(*) FROM chunks k JOIN sources s ON s.id = k.source_id"
                " WHERE k.collection_id = :c AND s.collection_id = :c"
            ),
            {"c": copy},
        )
    assert chunks and chunks >= 4


async def test_the_feed_reads_the_covers_setting_once_for_every_sharer(
    client: AsyncClient,
) -> None:
    async def feed() -> int:
        with recorded() as q:
            r = await client.get("/api/shares")
        assert r.status_code == 200, r.text
        return len(q)

    one = await other_person(client, "one@example.org")
    await _shared(client, 1, one)
    first = await feed()
    for n in range(3):
        await _shared(client, 1, await other_person(client, f"more{n}@example.org"))
    assert await feed() == first


async def test_a_searched_feed_and_its_items_cost_the_same_however_many_shares(
    client: AsyncClient,
) -> None:
    async def asking() -> int:
        with recorded() as q:
            for path in ("/api/shares", "/api/shares/items"):
                r = await client.get(path, params={"query": "copied", "limit": 2})
                assert r.status_code == 200, r.text
        return len(q)

    await _shared(client, 1, await other_person(client, "first@example.org"))
    first = await asking()
    for n in range(4):
        await _shared(client, 1, await other_person(client, f"next{n}@example.org"))
    assert await asking() == first
    r = await client.get("/api/shares", params={"query": "copied", "limit": 2})
    assert len(r.json()) == 2 and r.headers["x-next-offset"] == "2"
