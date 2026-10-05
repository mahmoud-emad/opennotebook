"""Retrieval, ported from opennotebook_memory's tests."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from opennotebook import memory
from opennotebook.db.models import User
from opennotebook.db.session import sessionmaker

KERNEL = (
    "# Scheduling\n\nThe scheduler picks the next process to run from the run queue "
    "each time a time slice ends.\n\n# Memory\n\nPage tables map virtual addresses to "
    "physical frames, and the TLB caches recent translations."
)
PRINTERS = "Printers were once shared through spooling daemons that queued jobs on disk."


async def _collection(client: AsyncClient, *notes: tuple[str, str]) -> uuid.UUID:
    cid = (await client.post("/api/collections", json={})).json()["id"]
    for title, body in notes:
        r = await client.post(
            f"/api/collections/{cid}/sources", json={"kind": "text", "text": body, "title": title}
        )
        assert r.status_code == 201, r.text
    return uuid.UUID(cid)


async def _search(cid: uuid.UUID, query: str, k: int = 3, **kw: object) -> list[memory.Hit]:
    async with sessionmaker()() as s:
        owner = (await s.scalars(select(User.id).where(User.email == "owner@localhost"))).first()
        assert owner is not None
        return await memory.search(s, owner, cid, query, k, **kw)  # pyright: ignore[reportArgumentType]


async def test_full_text_search_finds_the_passage_without_an_embedder(client: AsyncClient) -> None:
    cid = await _collection(client, ("kernel", KERNEL), ("printers", PRINTERS))
    hits = await _search(cid, "page tables TLB")
    assert hits[0].source == "kernel.md" and "TLB" in hits[0].text
    assert hits[0].score > 0, "fused scores are positive"
    # The offsets point at the passage in its source.
    stored = "# kernel\n\n" + KERNEL  # a titled note keeps its title as a heading
    assert stored[hits[0].start : hits[0].end] == hits[0].text
    assert await _search(cid, "quantum") == []
    assert await _search(cid, "?!") == []


async def test_collections_do_not_see_each_other(client: AsyncClient) -> None:
    a = await _collection(client, ("kernel", KERNEL), ("printers", PRINTERS))
    b = await _collection(client, ("other", "Nothing about printers or kernels at all here."))
    assert all(h.source == "other.md" for h in await _search(b, "spooling daemons", 5))
    assert {h.source for h in await _search(a, "spooling daemons", 5)} == {"printers.md"}
    # Only the named sources, when asked.
    assert await _search(a, "spooling daemons", 5, sources=["kernel.md"]) == []


async def test_a_removed_source_leaves_the_index(client: AsyncClient) -> None:
    cid = await _collection(client, ("zebras", "The old text mentions zebras."))
    assert len(await _search(cid, "zebras")) == 1
    await client.delete(f"/api/collections/{cid}/sources/zebras.md")
    assert await _search(cid, "zebras") == []


def test_query_syntax_is_never_passed_through() -> None:
    assert memory.ts_query("NEAR(a b) & title:x* | !y") == "near | title"
    assert memory.ts_query("a ! ?") is None


def test_agreement_between_rankings_wins() -> None:
    one, two, three = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fused = memory.fuse([[one, two], [three, two]], 3)
    assert fused[0][0] == two and len(fused) == 3


def test_passages_split_on_markdown_structure() -> None:
    text = ("# A\n\n" + "word " * 200 + "\n\n") * 3
    parts = memory.split(text)
    assert len(parts) >= 3
    assert all(text[start : start + len(t)] == t for start, t in parts)


@pytest.mark.parametrize("v", [[3.0, 4.0], [0.0, 0.0]])
def test_vectors_are_scaled_to_unit_length(v: list[float]) -> None:
    n = memory.normalized(v)
    assert n == ([0.6, 0.8] if any(v) else v)
