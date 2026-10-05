"""Discover's items: each output a share includes, listed on its own. The same
rules as a share's page: only what it includes, only what is there and ready,
only shares of people whose account is on."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.config import settings
from opennotebook.db.session import engine
from tests.conftest import other_person

T0 = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings(), "files_dir", tmp_path)
    return tmp_path


async def _me(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    return (await client.get("/api/me", headers=headers)).json()["id"]


async def _collection(
    client: AsyncClient, title: str, headers: dict[str, str] | None = None
) -> str:
    r = await client.post("/api/collections", json={"title": title}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _sql(sql: str, **kw: Any) -> Any:
    async with engine().begin() as c:
        r = await c.execute(text(sql), kw)
        return r.scalar() if r.returns_rows else None


async def _deck(owner: str, cid: str, title: str, at: int, kind: str = "slides") -> str:
    """A ready deck (or audio overview) of three parts, made `at` minutes after T0."""
    return str(
        await _sql(
            "INSERT INTO sessions (owner_id, collection_id, kind, title, state, slides,"
            " duration_ms, created_at) VALUES (:o, :c, :k, :t, 'ready',"
            " CAST(:slides AS jsonb), 522000, :at) RETURNING id",
            o=owner,
            c=cid,
            k=kind,
            t=title,
            slides='[{"title": "a"}, {"title": "b"}, {"title": "c"}]',
            at=T0 + timedelta(minutes=at),
        )
    )


async def _map(owner: str, cid: str, title: str, at: int) -> str:
    return str(
        await _sql(
            "INSERT INTO mindmaps (owner_id, collection_id, title, root, node_count, created_at)"
            " VALUES (:o, :c, :t, CAST(:root AS jsonb), 1, :at) RETURNING id",
            o=owner,
            c=cid,
            t=title,
            root='{"name": "Reefs", "children": []}',
            at=T0 + timedelta(minutes=at),
        )
    )


async def _notes(owner: str, cid: str, title: str, at: int) -> str:
    return str(
        await _sql(
            "INSERT INTO study_notes (owner_id, collection_id, title, body, created_at)"
            " VALUES (:o, :c, :t, CAST(:body AS jsonb), :at) RETURNING id",
            o=owner,
            c=cid,
            t=title,
            body='{"overview": "Reefs.", "ideas": []}',
            at=T0 + timedelta(minutes=at),
        )
    )


async def _share(
    client: AsyncClient,
    cid: str,
    outputs: list[str],
    note: str = "",
    headers: dict[str, str] | None = None,
) -> str:
    r = await client.post(
        f"/api/collections/{cid}/shares",
        json={"include_sources": False, "outputs": outputs, "note": note},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _items(
    client: AsyncClient, headers: dict[str, str] | None = None, **params: Any
) -> dict[str, Any]:
    r = await client.get("/api/shares/items", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _titles(page: dict[str, Any]) -> list[str]:
    return [i["title"] for i in page["items"]]


async def test_only_what_a_share_includes_and_is_ready_is_listed(client: AsyncClient) -> None:
    me = await _me(client)
    cid = await _collection(client, "Coral reefs")
    deck = await _deck(me, cid, "Reef deck", 1)
    audio = await _deck(me, cid, "Reef talk", 2, "audio")
    mid = await _map(me, cid, "Reef map", 3)
    nid = await _notes(me, cid, "Reef notes", 4)
    left_out = await _map(me, cid, "Not shared map", 5)
    sid = await _share(
        client, cid, [f"session:{deck}", f"session:{audio}", f"mindmap:{mid}", f"notes:{nid}"]
    )
    # A collection that is not shared lists nothing.
    other = await _collection(client, "Private")
    await _map(me, other, "Private map", 6)

    page = await _items(client)
    assert _titles(page) == ["Reef notes", "Reef map", "Reef talk", "Reef deck"]
    assert page["next_offset"] is None
    by = {i["title"]: i for i in page["items"]}
    assert "Not shared map" not in by and left_out
    d = by["Reef deck"]
    assert (d["kind"], d["key"], d["parts"], d["duration_ms"]) == (
        "slides",
        f"session:{deck}",
        3,
        522000,
    )
    assert (d["share_id"], d["collection_id"], d["collection_title"]) == (sid, cid, "Coral reefs")
    assert d["mine"] and d["reuses"] == 0
    assert (by["Reef talk"]["kind"], by["Reef map"]["kind"]) == ("audio", "mindmap")
    assert by["Reef map"]["key"] == f"mindmap:{mid}" and by["Reef map"]["parts"] == 0
    assert by["Reef notes"]["key"] == f"notes:{nid}"
    # The cover is the one the feed's card draws.
    (card,) = (await client.get("/api/shares")).json()
    assert {i["cover_version"] for i in page["items"]} == {card["cover_version"]}
    # Each opens through its share.
    assert (await client.get(f"/api/shares/{sid}/mindmaps/{mid}")).status_code == 200

    # No longer ready, or gone since it was shared: no longer listed.
    await _sql("UPDATE sessions SET state = 'failed' WHERE id = :id", id=deck)
    await _sql("DELETE FROM mindmaps WHERE id = :id", id=mid)
    assert _titles(await _items(client)) == ["Reef notes", "Reef talk"]
    # Taken out of the share: no longer listed.
    r = await client.patch(f"/api/shares/{sid}", json={"outputs": [f"notes:{nid}"]})
    assert r.status_code == 200, r.text
    assert _titles(await _items(client)) == ["Reef notes"]
    # The share stopped: nothing.
    assert (await client.delete(f"/api/shares/{sid}")).status_code == 204
    assert (await _items(client))["items"] == []


async def test_items_are_filtered_by_kind(client: AsyncClient) -> None:
    me = await _me(client)
    cid = await _collection(client, "Kelp")
    made = [
        f"session:{await _deck(me, cid, 'Deck', 1)}",
        f"session:{await _deck(me, cid, 'Talk', 2, 'audio')}",
        f"mindmap:{await _map(me, cid, 'Map', 3)}",
        f"notes:{await _notes(me, cid, 'Notes', 4)}",
    ]
    await _share(client, cid, made)
    for kind, title in (
        ("slides", "Deck"),
        ("audio", "Talk"),
        ("mindmap", "Map"),
        ("notes", "Notes"),
    ):
        page = await _items(client, kind=kind)
        assert _titles(page) == [title], kind
        assert page["items"][0]["kind"] == kind
    r = await client.get("/api/shares/items", params={"kind": "deck"})
    assert r.status_code == 422
    r = await client.get("/api/shares/items", params={"sort": "oldest"})
    assert r.status_code == 422


async def test_items_are_searched_by_their_title_and_their_share(client: AsyncClient) -> None:
    them = await other_person(client, "them@example.com")
    me = await _me(client)
    you = await _me(client, them)
    reefs = await _collection(client, "Coral reefs")
    kelp = await _collection(client, "Kelp forests", them)
    await _share(
        client,
        reefs,
        [
            f"mindmap:{await _map(me, reefs, 'Polyps', 1)}",
            f"notes:{await _notes(me, reefs, 'Bleaching', 2)}",
        ],
        note="for divers",
    )
    await _share(
        client,
        kelp,
        [f"mindmap:{await _map(you, kelp, 'Holdfasts', 3)}"],
        headers=them,
    )

    assert _titles(await _items(client, query="POLYP")) == ["Polyps"], "the item's own title"
    assert _titles(await _items(client, query="coral")) == ["Bleaching", "Polyps"], "its collection"
    assert _titles(await _items(client, query=" Divers ")) == ["Bleaching", "Polyps"], "its note"
    assert _titles(await _items(client, query="kelp", kind="mindmap")) == ["Holdfasts"]
    assert _titles(await _items(client, query="kelp", kind="notes")) == []
    assert (await _items(client, query="nothing like it"))["items"] == []
    # A search with the characters LIKE treats specially means them literally.
    assert (await _items(client, query="%"))["items"] == []


async def test_items_sort_newest_or_most_reused_and_page(client: AsyncClient) -> None:
    them = await other_person(client, "them@example.com")
    me = await _me(client)
    a = await _collection(client, "A")
    b = await _collection(client, "B")
    await _share(client, a, [f"mindmap:{await _map(me, a, f'a{n}', n)}" for n in range(3)])
    sb = await _share(
        client, b, [f"notes:{await _notes(me, b, f'b{n}', 10 + n)}" for n in range(2)]
    )

    assert _titles(await _items(client)) == ["b1", "b0", "a2", "a1", "a0"], "newest made first"
    # B is reused, so its items come first; newest still breaks the tie.
    r = await client.post(f"/api/shares/{sb}/reuse", headers=them)
    assert r.status_code == 201, r.text
    await _sql("UPDATE shares SET reuses = 0 WHERE collection_id = :c", c=a)
    page = await _items(client, sort="reused")
    assert _titles(page) == ["b1", "b0", "a2", "a1", "a0"]
    assert [i["reuses"] for i in page["items"]] == [1, 1, 0, 0, 0]
    await _sql("UPDATE shares SET reuses = 5 WHERE collection_id = :c", c=a)
    assert _titles(await _items(client, sort="reused")) == ["a2", "a1", "a0", "b1", "b0"]

    # In pages, each one telling where the next starts.
    first = await _items(client, limit=2)
    assert (_titles(first), first["next_offset"]) == (["b1", "b0"], 2)
    second = await _items(client, limit=2, offset=2)
    assert (_titles(second), second["next_offset"]) == (["a2", "a1"], 4)
    last = await _items(client, limit=2, offset=4)
    assert (_titles(last), last["next_offset"]) == (["a0"], None)
    assert (await _items(client, limit=2, offset=10))["items"] == []
    for bad in ({"limit": 0}, {"limit": 61}, {"offset": -1}):
        assert (await client.get("/api/shares/items", params=bad)).status_code == 422


async def test_everyone_sees_everyones_items_but_a_hidden_accounts(client: AsyncClient) -> None:
    them = await other_person(client, "them@example.com")
    me = await _me(client)
    you = await _me(client, them)
    mine = await _collection(client, "Mine")
    theirs = await _collection(client, "Theirs", them)
    await _share(client, mine, [f"mindmap:{await _map(me, mine, 'My map', 1)}"])
    await _share(
        client, theirs, [f"notes:{await _notes(you, theirs, 'Their notes', 2)}"], headers=them
    )
    await _sql("UPDATE users SET display_name = 'Sam' WHERE id = :id", id=you)

    for h, mine_flags in ((None, [False, True]), (them, [True, False])):
        page = await _items(client, headers=h)
        assert _titles(page) == ["Their notes", "My map"]
        assert [i["mine"] for i in page["items"]] == mine_flags
        assert page["items"][0]["shared_by"] == "Sam"

    # A person whose account is turned off is out, as from the feed.
    await _sql("UPDATE users SET disabled_at = now() WHERE id = :id", id=you)
    assert _titles(await _items(client)) == ["My map"]
    assert _titles(await _items(client, query="theirs")) == []
