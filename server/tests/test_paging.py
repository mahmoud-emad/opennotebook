"""Lists a page at a time: each answers with its usual array cut to `limit`,
and says where the next page starts while there is one."""

from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine
from tests.model import add_note

T0 = datetime(2026, 9, 1, tzinfo=UTC)


async def _me(client: AsyncClient) -> str:
    return (await client.get("/api/me")).json()["id"]


async def _pages(client: AsyncClient, path: str, key: str = "id") -> list[list[Any]]:
    """Every page of a list two at a time, following `X-Next-Offset`."""
    out: list[list[Any]] = []
    offset: int | None = 0
    while offset is not None:
        r = await client.get(path, params={"limit": 2, "offset": offset})
        assert r.status_code == 200, r.text
        out.append([x[key] for x in r.json()])
        nxt = r.headers.get("x-next-offset")
        offset = int(nxt) if nxt is not None else None
    return out


def _flat(pages: list[list[Any]]) -> list[Any]:
    return [x for page in pages for x in page]


async def _refused(client: AsyncClient, path: str, most: int) -> None:
    for bad in ({"limit": 0}, {"limit": most + 1}, {"offset": -1}, {"offset": 100_001}):
        assert (await client.get(path, params=bad)).status_code == 422, bad


async def test_collections_sources_outputs_and_keys_come_a_page_at_a_time(
    client: AsyncClient,
) -> None:
    ids = [
        (await client.post("/api/collections", json={"title": f"C{n}"})).json()["id"]
        for n in range(3)
    ]
    everything = [c["id"] for c in (await client.get("/api/collections")).json()]
    assert sorted(everything) == sorted(ids)
    assert _flat(await _pages(client, "/api/collections")) == everything
    assert [len(p) for p in await _pages(client, "/api/collections")] == [2, 1]
    await _refused(client, "/api/collections", 500)

    cid = ids[0]
    names = [
        await add_note(client, cid, f"Note {n} about reefs and the polyps that build them.")
        for n in range(3)
    ]
    assert _flat(await _pages(client, f"/api/collections/{cid}/sources", "name")) == names
    await _refused(client, f"/api/collections/{cid}/sources", 1000)

    me = await _me(client)
    async with engine().begin() as c:
        for n in range(3):
            await c.execute(
                text(
                    "INSERT INTO sessions (owner_id, collection_id, kind, title, state, created_at)"
                    " VALUES (:o, :c, 'slides', :t, 'ready', :at)"
                ),
                {"o": me, "c": cid, "t": f"Deck {n}", "at": T0 + timedelta(minutes=n)},
            )
    pages = await _pages(client, "/api/sessions", "title")
    assert pages == [["Deck 2", "Deck 1"], ["Deck 0"]], "newest first"
    await _refused(client, "/api/sessions", 500)

    for n in range(3):
        assert (await client.post("/api/keys", json={"name": f"k{n}"})).status_code == 201
    keys = _flat(await _pages(client, "/api/keys", "name"))
    assert sorted(keys) == ["k0", "k1", "k2"] and len(keys) == 3
    await _refused(client, "/api/keys", 500)


async def test_a_conversation_reads_from_its_newest_message_back(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Talk"})).json()["id"]
    me = await _me(client)
    async with engine().begin() as c:
        for n in range(5):
            await c.execute(
                text(
                    "INSERT INTO chat_messages (owner_id, collection_id, role, text, created_at)"
                    " VALUES (:o, :c, :r, :t, :at)"
                ),
                {
                    "o": me,
                    "c": cid,
                    "r": "user" if n % 2 == 0 else "assistant",
                    "t": f"m{n}",
                    "at": T0 + timedelta(seconds=n),
                },
            )
    # Unasked, the whole conversation, oldest first, as before.
    r = await client.get(f"/api/collections/{cid}/chat")
    assert [m["text"] for m in r.json()] == ["m0", "m1", "m2", "m3", "m4"]
    assert "x-next-before" not in r.headers

    seen: list[list[str]] = []
    params: dict[str, Any] = {"limit": 2}
    while True:
        r = await client.get(f"/api/collections/{cid}/chat", params=params)
        assert r.status_code == 200, r.text
        seen.append([m["text"] for m in r.json()])
        if (before := r.headers.get("x-next-before")) is None:
            break
        assert before == r.json()[0]["id"], "the oldest message of the page"
        params = {"limit": 2, "before": before}
    assert seen == [["m3", "m4"], ["m1", "m2"], ["m0"]]

    r = await client.get(
        f"/api/collections/{cid}/chat",
        params={"before": "00000000-0000-7000-8000-000000000000"},
    )
    assert r.status_code == 404
    assert r.json()["detail"].startswith("That message is no longer in the conversation")
    for bad in ({"limit": 0}, {"limit": 501}, {"before": "not an id"}):
        r = await client.get(f"/api/collections/{cid}/chat", params=bad)
        assert r.status_code == 422, bad
