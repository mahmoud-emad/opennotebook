"""Lists read what they show and no more: a source's text, a deck's lines, a
map's tree and a set of notes stay in the database until one is opened; a
check that a collection is there reads its row, not its counts; and the cost
of starting a collection does not grow with how many a person has."""

import json
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import covers, reading
from tests.model import add_note
from tests.queries import recorded

LONG = "Coral reefs are built by colonies of tiny polyps over many centuries. " * 50


async def _me(client: AsyncClient) -> str:
    return (await client.get("/api/me")).json()["id"]


async def _outputs(owner: str, cid: str) -> None:
    """A deck with a line, a map and notes, written directly."""
    slides = [{"title": "Intro", "lines": [{"line_id": "l0", "text": "Hello"}]}, {"title": "Two"}]
    body = {
        "overview": "o",
        "ideas": [{"heading": "One", "body": "b"}, {"heading": "Two", "body": "b"}],
        "quiz": [{"question": "q", "answer": "a"}],
        "glossary": [],
    }
    async with engine().begin() as c:
        p = {"o": owner, "c": cid}
        await c.execute(
            text(
                "INSERT INTO sessions (owner_id, collection_id, kind, title, state, slides)"
                " VALUES (:o, :c, 'slides', 'Deck', 'ready', CAST(:s AS jsonb))"
            ),
            {**p, "s": json.dumps(slides)},
        )
        await c.execute(
            text(
                "INSERT INTO mindmaps (owner_id, collection_id, title, root, node_count, shape)"
                " VALUES (:o, :c, 'Map', CAST(:r AS jsonb), 2, '{1}')"
            ),
            {**p, "r": json.dumps({"name": "Reefs", "children": [{"name": "Polyps"}]})},
        )
        await c.execute(
            text(
                "INSERT INTO study_notes (owner_id, collection_id, title, body)"
                " VALUES (:o, :c, 'Notes', CAST(:b AS jsonb))"
            ),
            {**p, "b": json.dumps(body)},
        )


async def _collection(client: AsyncClient) -> str:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    await add_note(client, cid, LONG, "Reefs")
    await _outputs(await _me(client), cid)
    return cid


async def test_lists_leave_text_lines_trees_and_bodies_in_the_database(
    client: AsyncClient,
) -> None:
    cid = await _collection(client)

    with recorded() as q:
        sources = (await client.get(f"/api/collections/{cid}/sources")).json()
    assert sources[0]["chars"] > 1000
    assert not q.loads("sources.text")

    with recorded() as q:
        detail = (await client.get(f"/api/collections/{cid}")).json()
        listed = (await client.get("/api/sessions")).json()
    assert [o["parts"] for o in detail["outputs"]] == [2]
    assert [o["parts"] for o in listed] == [2]
    assert not q.loads("sessions.slides")

    with recorded() as q:
        maps = (await client.get(f"/api/collections/{cid}/mindmaps")).json()
    assert maps[0]["shape"] == [1] and maps[0]["node_count"] == 2
    assert not q.loads("mindmaps.root")

    with recorded() as q:
        notes = (await client.get(f"/api/collections/{cid}/notes")).json()
    assert (notes[0]["ideas"], notes[0]["questions"], notes[0]["terms"]) == (2, 1, 0)
    assert notes[0]["headings"] == ["One", "Two"]
    assert not q.loads("study_notes.body")

    # Opening one still reads it whole.
    one = (await client.get(f"/api/collections/{cid}/notes/{notes[0]['id']}")).json()
    assert [i["heading"] for i in one["idea_list"]] == ["One", "Two"]


async def test_a_shares_page_and_dialog_leave_the_text_and_parts_behind(
    client: AsyncClient,
) -> None:
    cid = await _collection(client)
    with recorded() as q:
        state = (await client.get(f"/api/collections/{cid}/share")).json()
    assert state["sources"] == 1 and len(state["items"]) == 3
    assert not any(q.loads(c) for c in ("sessions.slides", "mindmaps.root", "study_notes.body"))
    share = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": True, "outputs": state["picked"]},
        )
    ).json()
    with recorded() as q:
        page = await client.get(f"/api/shares/{share['id']}")
    assert page.status_code == 200, page.text
    assert not q.loads("sources.text")


async def test_a_cover_check_reads_ids_and_titles_only(client: AsyncClient) -> None:
    cid = await _collection(client)
    owner = await _me(client)
    async with sessionmaker()() as s:
        with recorded() as q:
            await covers.gather(s, uuid.UUID(owner), [uuid.UUID(cid)])
    assert not any(q.loads(c) for c in ("sessions.slides", "mindmaps.root", "study_notes.body"))


async def test_only_the_sources_named_are_read(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Two"})).json()["id"]
    first = await add_note(client, cid, LONG, "First")
    await add_note(client, cid, "Kelp forests grow in cold water along rocky coasts.", "Second")
    owner = await _me(client)
    async with sessionmaker()() as s:
        with recorded() as q:
            docs = await reading.read_docs(s, uuid.UUID(owner), uuid.UUID(cid), [first])
    assert [d.name for d in docs] == [first]
    read = q.selecting("FROM sources")
    assert len(read) == 1 and " IN " in read[0].upper()


async def _creating(client: AsyncClient) -> int:
    with recorded() as q:
        r = await client.post("/api/collections", json={})
    assert r.status_code == 201, r.text
    await client.delete(f"/api/collections/{r.json()['id']}")
    return len(q)


async def test_starting_a_collection_costs_the_same_however_many_there_are(
    client: AsyncClient,
) -> None:
    before = await _creating(client)
    for n in range(4):
        cid = (await client.post("/api/collections", json={"title": f"Full {n}"})).json()["id"]
        await add_note(client, cid, LONG, f"Note {n}")
    assert await _creating(client) == before


async def test_a_check_that_a_collection_is_there_reads_no_counts(client: AsyncClient) -> None:
    cid = await _collection(client)
    with recorded() as q:
        r = await client.get(f"/api/collections/{cid}/mindmaps")
    assert r.status_code == 200
    # The collection's own row, never its summary's counts of what it holds.
    assert not any("count(" in s.lower() for s in q.selecting("FROM collections"))
    other: Any = await client.get(f"/api/collections/{'0' * 8}-0000-0000-0000-{'0' * 12}/notes")
    assert other.status_code == 404
    assert other.json()["detail"].startswith("That collection is no longer there.")
