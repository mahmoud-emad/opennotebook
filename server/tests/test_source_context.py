"""A source switched off stays in its collection but is read by nothing."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from opennotebook import memory
from opennotebook.db.models import User
from opennotebook.db.session import sessionmaker
from tests.conftest import other_person
from tests.model import add_note, install, says

REEFS = (
    "Coral reefs cover less than one percent of the ocean floor "
    "but shelter a quarter of marine species."
)
KELP = "Kelp forests grow along cold coasts and shelter fish, otters and sea urchins."


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def _switch(client: AsyncClient, cid: str, name: str, active: bool) -> object:
    return await client.patch(f"/api/collections/{cid}/sources/{name}", json={"active": active})


async def test_a_source_is_switched_off_and_on_and_listed_so(client: AsyncClient) -> None:
    cid = await _collection(client)
    name = await add_note(client, cid, REEFS)
    listed = (await client.get(f"/api/collections/{cid}/sources")).json()
    assert listed[0]["active"] is True

    r = await _switch(client, cid, name, False)
    assert r.status_code == 200 and r.json()["active"] is False  # type: ignore[attr-defined]
    listed = (await client.get(f"/api/collections/{cid}/sources")).json()
    assert [(s["name"], s["active"]) for s in listed] == [(name, False)]
    # Still there to read, and still counted as the collection's.
    assert (await client.get(f"/api/collections/{cid}/sources/{name}")).json()["text"] == REEFS

    r = await _switch(client, cid, name, True)
    assert r.json()["active"] is True  # type: ignore[attr-defined]


async def test_an_unknown_source_and_a_stranger_are_not_found(client: AsyncClient) -> None:
    cid = await _collection(client)
    name = await add_note(client, cid, REEFS)
    r = await _switch(client, cid, "nope.md", False)
    assert r.status_code == 404  # type: ignore[attr-defined]
    stranger = await other_person(client, "stranger@example.org")
    r = await client.patch(
        f"/api/collections/{cid}/sources/{name}", json={"active": False}, headers=stranger
    )
    assert r.status_code == 404
    assert (await client.get(f"/api/collections/{cid}/sources")).json()[0]["active"] is True


async def test_ask_leaves_out_a_source_that_is_off(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    kelp = await add_note(client, cid, KELP)
    reefs = (await client.get(f"/api/collections/{cid}/sources")).json()[0]["name"]
    await _switch(client, cid, reefs, False)

    model.answers(says("Otters [1]."))
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "What lives there?"})
    assert r.status_code == 200, r.text
    assert "Coral" not in model.said_to(0, "user")
    assert r.json()["citations"][0]["name"] == kelp


async def test_naming_a_source_that_is_off_is_refused_with_the_way_out(
    client: AsyncClient,
) -> None:
    cid = await _collection(client)
    name = await add_note(client, cid, REEFS)
    await _switch(client, cid, name, False)
    r = await client.post(
        f"/api/collections/{cid}/ask", json={"question": "Reefs?", "sources": [name]}
    )
    assert r.status_code == 422
    assert "switched off" in r.json()["detail"] and "Switch it on" in r.json()["detail"]


async def test_when_every_source_is_off_the_error_says_so(client: AsyncClient) -> None:
    cid = await _collection(client)
    name = await add_note(client, cid, REEFS)
    await _switch(client, cid, name, False)
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "Reefs?"})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Every source of this collection is switched off")


async def test_search_leaves_out_a_source_that_is_off(client: AsyncClient) -> None:
    cid = uuid.UUID(await _collection(client))
    name = await add_note(client, str(cid), REEFS)
    await add_note(client, str(cid), KELP)
    await _switch(client, str(cid), name, False)
    async with sessionmaker()() as s:
        owner = (await s.scalars(select(User.id).where(User.email == "owner@localhost"))).first()
        assert owner is not None
        hits = await memory.search(s, owner, cid, "shelter species", 5)
    assert hits and all(h.source != name for h in hits)
