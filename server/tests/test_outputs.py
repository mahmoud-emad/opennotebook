"""Outputs, mind maps, notes and the conversation, read and managed. Their
making is ported later; rows are written directly here."""

import json

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


async def test_untitled_outputs_are_shown_by_what_they_are(client: AsyncClient) -> None:
    cid, owner = await _setup(client)
    async with engine().begin() as c:
        for kind in ("slides", "audio"):
            await c.execute(
                text(
                    "INSERT INTO sessions (owner_id, collection_id, kind, title, state, slides,"
                    " speakers) VALUES (:o, :c, :k, '', 'ready', '[]', '[]')"
                ),
                {"o": owner, "c": cid, "k": kind},
            )
        await c.execute(
            text(
                "INSERT INTO mindmaps (owner_id, collection_id, title, root, node_count)"
                ' VALUES (:o, :c, \' \', \'{"name": "Reefs", "children": []}\', 1)'
            ),
            {"o": owner, "c": cid},
        )
        await c.execute(
            text(
                "INSERT INTO study_notes (owner_id, collection_id, title, body)"
                " VALUES (:o, :c, '', '{}')"
            ),
            {"o": owner, "c": cid},
        )
    outs = (await client.get(f"/api/collections/{cid}")).json()["outputs"]
    # Not "Untitled collection": an untitled deck is said as a deck.
    assert sorted(o["display_title"] for o in outs) == [
        "Untitled audio overview",
        "Untitled narrated slides",
    ]
    maps = (await client.get(f"/api/collections/{cid}/mindmaps")).json()
    notes = (await client.get(f"/api/collections/{cid}/notes")).json()
    assert (maps[0]["display_title"], notes[0]["display_title"]) == (
        "Untitled mind map",
        "Untitled study notes",
    )


async def test_a_failure_is_a_sentence_with_its_detail_apart(client: AsyncClient) -> None:
    cid, owner = await _setup(client)
    said = "The AI account is out of credit. Add credit, then try again."
    async with engine().begin() as c:
        sid = (
            await c.execute(
                text(
                    "INSERT INTO sessions (owner_id, collection_id, kind, title, state, failure,"
                    " slides, speakers) VALUES (:o, :c, 'slides', 'Deck', 'failed', :f, '[]',"
                    " CAST(:sp AS jsonb))"
                    " RETURNING id"
                ),
                {
                    "o": owner,
                    "c": cid,
                    "f": said,
                    "sp": json.dumps(
                        [{"speaker_id": "host", "voice_id": "af_bella", "display_name": "Host"}]
                    ),
                },
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO jobs (owner_id, kind, status, error, session_id, collection_id)"
                " VALUES (:o, 'prep', 'failed', :e, :s, :c)"
            ),
            {"o": owner, "c": cid, "s": sid, "e": "HTTP 402: insufficient_quota"},
        )
        old = (
            await c.execute(
                text(
                    "INSERT INTO sessions (owner_id, collection_id, kind, title, state, failure,"
                    " slides, speakers) VALUES (:o, :c, 'slides', 'Old', 'failed',"
                    " 'deck came back failed after 600s (0/5 rendered)', '[]', '[]') RETURNING id"
                ),
                {"o": owner, "c": cid},
            )
        ).scalar_one()
    outs = {o["id"]: o for o in (await client.get(f"/api/collections/{cid}")).json()["outputs"]}
    assert (outs[str(sid)]["failure"], outs[str(sid)]["failure_detail"]) == (
        said,
        "HTTP 402: insufficient_quota",
    )
    # An older row's raw words are kept back as the detail.
    assert outs[str(old)]["failure"].startswith("Something went wrong while making this.")
    assert outs[str(old)]["failure_detail"] == "deck came back failed after 600s (0/5 rendered)"
    full = (await client.get(f"/api/sessions/{sid}")).json()
    assert full["failure_detail"] == "HTTP 402: insufficient_quota"
    # A role word for a name: the speaker is called by their voice.
    assert full["speaker_names"] == {"host": "Bella"}
