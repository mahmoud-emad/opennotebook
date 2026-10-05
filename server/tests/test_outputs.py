"""Outputs, mind maps, notes and the conversation, read and managed. Their
making is ported later; rows are written directly here."""

from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine


async def _setup(client: AsyncClient) -> tuple[str, str]:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    owner = (await client.get("/api/me")).json()["id"]
    return cid, owner


async def test_an_output_is_read_renamed_played_and_deleted(client: AsyncClient) -> None:
    cid, owner = await _setup(client)
    async with engine().begin() as c:
        sid = (
            await c.execute(
                text(
                    "INSERT INTO sessions"
                    " (owner_id, collection_id, kind, title, state, slides, speakers)"
                    " VALUES (:o, :c, 'slides', 'Deck', 'ready',"
                    ' \'[{"title": "One", "lines": []}]\', \'[{"speaker_id": "a"}]\')'
                    " RETURNING id"
                ),
                {"o": owner, "c": cid},
            )
        ).scalar_one()

    out = (await client.get(f"/api/collections/{cid}")).json()
    assert out["collection"]["decks"] == 1 and out["outputs"][0]["parts"] == 1

    full = (await client.get(f"/api/sessions/{sid}")).json()
    assert full["slides"][0]["title"] == "One" and full["speakers"] == 1

    r = await client.patch(f"/api/sessions/{sid}", json={"title": "Reef deck", "pinned": True})
    assert r.json()["title"] == "Reef deck" and r.json()["pinned"]

    assert (await client.get(f"/api/sessions/{sid}/playback")).json()["state"] == "idle"
    at = {"slide_ordinal": 2, "line_id": "l3", "offset_ms": 1200, "state": "paused"}
    assert (await client.put(f"/api/sessions/{sid}/playback", json=at)).json() == at
    assert (await client.get(f"/api/sessions/{sid}/playback")).json() == at

    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 204
    assert (await client.get(f"/api/sessions/{sid}")).status_code == 404


async def test_maps_and_notes_are_listed_and_renamed(client: AsyncClient) -> None:
    cid, owner = await _setup(client)
    async with engine().begin() as c:
        mid = (
            await c.execute(
                text(
                    "INSERT INTO mindmaps (owner_id, collection_id, title, root, node_count, shape)"
                    " VALUES (:o, :c, 'Map', '{\"name\": \"Reefs\", \"children\": []}', 1, '{2,3}')"
                    " RETURNING id"
                ),
                {"o": owner, "c": cid},
            )
        ).scalar_one()
        nid = (
            await c.execute(
                text(
                    "INSERT INTO study_notes (owner_id, collection_id, title, body) VALUES (:o, :c,"
                    ' \'Notes\', \'{"ideas": [{"heading": "Polyps", "body": "b"}], "quiz": []}\')'
                    " RETURNING id"
                ),
                {"o": owner, "c": cid},
            )
        ).scalar_one()

    maps = (await client.get(f"/api/collections/{cid}/mindmaps")).json()
    assert maps[0]["shape"] == [2, 3]
    m = (await client.get(f"/api/collections/{cid}/mindmaps/{mid}")).json()
    assert m["root"]["name"] == "Reefs"
    r = await client.patch(f"/api/collections/{cid}/mindmaps/{mid}", json={"title": "Reef map"})
    assert r.json()["title"] == "Reef map"

    notes = (await client.get(f"/api/collections/{cid}/notes")).json()
    assert notes[0]["headings"] == ["Polyps"] and notes[0]["ideas"] == 1
    n = (await client.get(f"/api/collections/{cid}/notes/{nid}")).json()
    assert n["idea_list"][0]["heading"] == "Polyps"

    summary = (await client.get(f"/api/collections/{cid}")).json()["collection"]
    assert summary["maps"] == 1 and summary["notes"] == 1
    assert (await client.delete(f"/api/collections/{cid}/mindmaps/{mid}")).status_code == 204
    assert (await client.delete(f"/api/collections/{cid}/notes/{nid}")).status_code == 204


async def test_the_conversation_is_kept_and_cleared(client: AsyncClient) -> None:
    cid, owner = await _setup(client)
    async with engine().begin() as c:
        for role, said in [("user", "What are reefs?"), ("assistant", "Colonies of polyps [1].")]:
            await c.execute(
                text(
                    "INSERT INTO chat_messages (owner_id, collection_id, role, text)"
                    " VALUES (:o, :c, :r, :t)"
                ),
                {"o": owner, "c": cid, "r": role, "t": said},
            )
    chat = (await client.get(f"/api/collections/{cid}/chat")).json()
    assert [m["role"] for m in chat] == ["user", "assistant"]
    assert (await client.delete(f"/api/collections/{cid}/chat")).status_code == 204
    assert (await client.get(f"/api/collections/{cid}/chat")).json() == []

    commands = [c["name"] for c in (await client.get("/api/commands")).json()]
    assert commands[:4] == ["slides", "audio", "mindmap", "notes"] and "help" in commands
