import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.api.collections import events_of
from opennotebook.db.session import engine
from opennotebook.jobs.events import hub
from tests.conftest import other_person
from tests.model import add_note


async def test_a_collection_is_made_listed_renamed_pinned_and_deleted(client: AsyncClient) -> None:
    r = await client.post("/api/collections", json={"title": "  Coral   reefs "})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["title"] == "Coral reefs" and not c["title_auto"] and c["sources"] == 0

    untitled = (await client.post("/api/collections", json={})).json()
    assert untitled["title"] == "" and untitled["title_auto"]

    listed = (await client.get("/api/collections")).json()
    # Most recently updated first.
    assert [x["id"] for x in listed] == [untitled["id"], c["id"]]

    r = await client.patch(f"/api/collections/{c['id']}", json={"title": "Reefs", "pinned": True})
    assert r.json()["title"] == "Reefs" and r.json()["pinned"]
    # An empty title hands the naming back to the studio.
    r = await client.patch(f"/api/collections/{c['id']}", json={"title": " "})
    assert r.json()["title_auto"]

    detail = (await client.get(f"/api/collections/{c['id']}")).json()
    assert detail["collection"]["id"] == c["id"] and detail["outputs"] == []

    assert (await client.delete(f"/api/collections/{c['id']}")).status_code == 204
    r = await client.get(f"/api/collections/{c['id']}")
    assert r.status_code == 404
    assert r.json() == {
        "detail": "That collection is no longer there. Reload the page to see what is."
    }


async def test_a_sixth_empty_collection_is_refused_in_words(client: AsyncClient) -> None:
    ids = [(await client.post("/api/collections", json={})).json()["id"] for _ in range(5)]
    r = await client.post("/api/collections", json={})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("You already have 5 empty collections.")

    # A collection with a source is not empty, so one more may start.
    note = {"kind": "text", "text": "Reefs are built by colonies of coral polyps over centuries."}
    assert (await client.post(f"/api/collections/{ids[0]}/sources", json=note)).status_code == 201
    assert (await client.post("/api/collections", json={})).status_code == 201


async def test_the_limit_counts_per_person(client: AsyncClient) -> None:
    for _ in range(5):
        await client.post("/api/collections", json={})
    them = await other_person(client, "them@example.com")
    assert (await client.post("/api/collections", json={}, headers=them)).status_code == 201


async def test_one_person_cannot_reach_anothers_collection(client: AsyncClient) -> None:
    mine = (await client.post("/api/collections", json={"title": "Mine"})).json()
    them = await other_person(client, "them@example.com")

    assert (await client.get("/api/collections", headers=them)).json() == []
    for method, path, body in [
        ("GET", f"/api/collections/{mine['id']}", None),
        ("PATCH", f"/api/collections/{mine['id']}", {"title": "Theirs"}),
        ("DELETE", f"/api/collections/{mine['id']}", None),
        ("GET", f"/api/collections/{mine['id']}/sources", None),
        ("GET", f"/api/collections/{mine['id']}/chat", None),
        ("GET", f"/api/collections/{mine['id']}/mindmaps", None),
        ("GET", f"/api/collections/{mine['id']}/notes", None),
        ("POST", f"/api/collections/{mine['id']}/sources", {"kind": "text", "text": "x" * 80}),
    ]:
        r = await client.request(method, path, json=body, headers=them)
        assert r.status_code == 404, (method, path, r.text)
    assert (await client.get(f"/api/collections/{mine['id']}")).json()["collection"][
        "title"
    ] == "Mine"


async def test_a_bad_request_is_answered_in_a_sentence(client: AsyncClient) -> None:
    r = await client.post("/api/collections", json={"title": "x" * 300})
    assert r.status_code == 422
    assert r.json()["detail"] == "Title: string should have at most 200 characters."
    r = await client.get("/api/collections/not-a-uuid")
    assert r.status_code == 422 and r.json()["detail"].startswith("Cid:")
    r = await client.get("/api/nowhere")
    assert r.json()["detail"] == "That is no longer there. Reload the page and try again."


# ── what a page needs to know, and following one ─────────────────────────────


async def _queue_done() -> None:
    """The worker finished every job: what the queue says once it has."""
    async with engine().begin() as c:
        await c.execute(text("UPDATE procrastinate_jobs SET status = 'succeeded'"))


async def test_a_collection_says_its_name_and_whether_it_is_still_being_made(
    client: AsyncClient,
) -> None:
    c = (await client.post("/api/collections", json={})).json()
    assert (c["display_title"], c["auto_named"], c["name_note"], c["busy"]) == (
        "Untitled collection",
        True,
        None,
        False,
    )
    # A source in, its name and cover asked for: busy until the worker is done.
    await add_note(client, c["id"], "Coral reefs are built by polyps. " * 10)
    got = (await client.get(f"/api/collections/{c['id']}")).json()["collection"]
    assert got["busy"] and got["name_note"] == "Naming it from its sources…"
    await _queue_done()
    got = (await client.get("/api/collections")).json()[0]
    assert not got["busy"]
    # Named by a person: the studio leaves the name alone.
    got = (await client.patch(f"/api/collections/{c['id']}", json={"title": "Reefs"})).json()
    assert (got["display_title"], got["auto_named"], got["name_note"]) == ("Reefs", False, None)
    # Handed back, with naming off in Settings: still the person's to name.
    r = await client.patch("/api/settings/OPENNOTEBOOK_AUTO_NAME", json={"value": "off"})
    assert r.status_code == 200, r.text
    got = (await client.patch(f"/api/collections/{c['id']}", json={"title": ""})).json()
    assert got["title_auto"] and not got["auto_named"] and got["name_note"] is None


async def _next(ev: AsyncIterator[Any]) -> tuple[str, Any]:
    """The next event that is not a keep-alive."""
    while True:
        e = await asyncio.wait_for(anext(ev), 10)
        if e is not None:
            return e


async def test_a_collection_is_followed_as_it_changes_until_it_is_gone(
    client: AsyncClient,
) -> None:
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    ev = events_of(owner, uuid.UUID(cid))
    try:
        first = [await _next(ev) for _ in range(5)]
        # Everything when it starts, so a page that connects late misses nothing.
        assert [n for n, _ in first] == ["collection", "outputs", "sources", "mindmaps", "notes"]
        assert first[0][1]["display_title"] == "Reefs"
        assert [d for _, d in first[1:]] == [[], [], [], []]

        await add_note(client, cid, "Coral reefs are built by polyps. " * 10, title="Polyps")
        got: dict[str, Any] = {}
        while "sources" not in got:
            name, data = await _next(ev)
            got[name] = data
        assert [x["title"] for x in got["sources"]] == ["Polyps"]

        assert (await client.delete(f"/api/collections/{cid}")).status_code == 204
        while (e := await _next(ev))[0] != "gone":
            pass
        assert e == ("gone", {"collection_id": cid})
    finally:
        await ev.aclose()
        await hub.close()


async def test_only_ones_own_collection_is_followed(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    them = await other_person(client, "them@test")
    assert (await client.get(f"/api/collections/{cid}/events", headers=them)).status_code == 404
    assert (await client.get(f"/api/collections/{uuid.uuid4()}/events")).status_code == 404
