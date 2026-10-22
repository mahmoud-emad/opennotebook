"""Searching a collection's sources."""

from httpx import AsyncClient

from tests.conftest import other_person
from tests.model import add_note

KERNEL = (
    "# Scheduling\n\nThe scheduler picks the next process to run from the run queue "
    "each time a time slice ends.\n\n# Memory\n\nPage tables map virtual addresses to "
    "physical frames, and the TLB caches recent translations."
)
PRINTERS = "Printers were once shared through spooling daemons that queued jobs on disk."


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def test_a_search_finds_the_passage_and_names_its_source(client: AsyncClient) -> None:
    cid = await _collection(client)
    kernel = await add_note(client, cid, KERNEL, "Kernel")
    await add_note(client, cid, PRINTERS, "Printers")
    r = await client.get(f"/api/collections/{cid}/search", params={"q": "page tables TLB"})
    assert r.status_code == 200, r.text
    (hit, *_) = r.json()
    assert hit["name"] == kernel and hit["title"] == "Kernel"
    assert "Page tables" in hit["excerpt"]
    # The offsets point at the passage in the source's own text.
    text = (await client.get(f"/api/collections/{cid}/sources/{kernel}")).json()["text"]
    assert text[hit["start"] : hit["end"]] == hit["excerpt"]


async def test_a_source_that_is_off_is_not_searched(client: AsyncClient) -> None:
    cid = await _collection(client)
    kernel = await add_note(client, cid, KERNEL, "Kernel")
    await client.patch(f"/api/collections/{cid}/sources/{kernel}", json={"active": False})
    r = await client.get(f"/api/collections/{cid}/search", params={"q": "scheduler"})
    assert r.status_code == 200 and r.json() == []


async def test_the_limit_caps_the_hits(client: AsyncClient) -> None:
    cid = await _collection(client)
    for i in range(3):
        await add_note(client, cid, f"The scheduler number {i} runs processes.", f"S{i}")
    r = await client.get(f"/api/collections/{cid}/search", params={"q": "scheduler", "limit": 2})
    assert len(r.json()) == 2
    r = await client.get(f"/api/collections/{cid}/search", params={"q": "scheduler", "limit": 99})
    assert r.status_code == 422


async def test_an_empty_search_says_what_to_do(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.get(f"/api/collections/{cid}/search", params={"q": "  "})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("The search is empty. Type a word")


async def test_a_stranger_cannot_search_a_collection(client: AsyncClient) -> None:
    cid = await _collection(client)
    await add_note(client, cid, KERNEL, "Kernel")
    stranger = await other_person(client, "stranger@example.org")
    r = await client.get(
        f"/api/collections/{cid}/search", params={"q": "scheduler"}, headers=stranger
    )
    assert r.status_code == 404
