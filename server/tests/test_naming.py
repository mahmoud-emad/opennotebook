"""Naming a collection from its sources. Ported from the naming tests of the
Rust server's `collection.rs`, with the background run a source change
starts."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine
from opennotebook.domain import covers, naming, refresh
from opennotebook.domain import settings as st
from tests.fake_ai import FakeModel


def test_a_model_reply_is_cleaned_into_a_name_or_refused() -> None:
    assert naming.clean_title('"Linux Kernel Internals".') == "Linux Kernel Internals"
    assert naming.clean_title("Title: **Speech Models**\n") == "Speech Models"
    assert naming.clean_title("\n\n# Rust Async\n") == "Rust Async"
    assert naming.clean_title("“Kernel Scheduling”.") == "Kernel Scheduling", (
        "curly quotes are quotes too"
    )
    assert naming.clean_title("‘Rust Lifetimes’") == "Rust Lifetimes"
    assert naming.clean_title("   ") is None
    assert (
        naming.clean_title(
            "These sources together discuss a number of different things about the kernel "
            "and its history"
        )
        is None
    ), "a sentence is not a name"


def test_the_heuristic_title_is_the_first_sources_clipped() -> None:
    read = [
        naming.Opening('"Quantifying Variance in Evaluation Benchmarks"', "x"),
        naming.Opening("Other", "y"),
    ]
    assert naming.heuristic_title(read) == "Quantifying Variance in Evaluation Benchmarks"
    long = [
        naming.Opening(
            "Moshi: a speech-text foundation model for real-time dialogue, and more besides", ""
        )
    ]
    assert (
        naming.heuristic_title(long)
        == "Moshi: a speech-text foundation model for real-time dialogue, and more"
    )
    assert naming.heuristic_title([]) == ""


def test_the_naming_prompt_reads_past_the_heading_and_source_line() -> None:
    o = naming.opening("# Tour\n\nSource: https://x\n\nThe kernel   schedules\ntasks.")
    assert o == "The kernel schedules tasks."
    # A long source is read only as far as its opening.
    assert len(naming.opening("# T\n" + "word " * 100_000)) == naming.NAME_OPENING_CHARS


def test_a_naming_run_asked_for_twice_runs_again_not_beside_itself() -> None:
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    running: dict[uuid.UUID, bool] = {}
    assert refresh.run_claim(running, s1), "the first caller runs it"
    assert not refresh.run_claim(running, s1), "a second waits on it"
    assert not refresh.run_claim(running, s1), "and a third"
    assert refresh.run_claim(running, s2), "another collection is not held up"
    assert refresh.run_done(running, s1), "asked again: once more"
    assert not refresh.run_done(running, s1), "nothing new: done"
    assert refresh.run_claim(running, s1), "free to run again"


# ── in the background, after a source change ──────────────────────────────────

NOTE = "Coral reefs are built by colonies of tiny polyps over many centuries."
PAGE = "# Reef Ecology\n\nSource: https://example.org/reefs\n\nReefs shelter a quarter of sea life."


async def _add(client: AsyncClient, cid: str, text: str, title: str = "") -> str:
    r = await client.post(
        f"/api/collections/{cid}/sources", json={"kind": "text", "text": text, "title": title}
    )
    assert r.status_code == 201, r.text
    await refresh.settle()
    return r.json()[0]["source"]["name"]


async def _title(client: AsyncClient, cid: str) -> str:
    return (await client.get(f"/api/collections/{cid}")).json()["collection"]["title"]


async def test_a_collection_is_named_by_the_model_from_its_sources(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModel('"Coral Reef Ecology".')
    monkeypatch.setattr(naming, "ai", model)
    cid = (await client.post("/api/collections", json={})).json()["id"]
    await _add(client, cid, PAGE, "Reef Ecology")
    assert await _title(client, cid) == "Coral Reef Ecology"
    assert len(model.asked) == 1
    user = model.asked[0]["messages"][1]["content"]
    # The page's heading and Source line are in the prompt once, as its title.
    assert user == "Source: Reef Ecology\nReefs shelter a quarter of sea life."
    async with engine().begin() as c:
        kinds = (await c.execute(text("SELECT kind FROM usage_events"))).scalars().all()
    assert "title" in kinds, "the call is on the ledger"

    # The same sources again ask no model.
    refresh.spawn(await _owner(cid), uuid.UUID(cid))
    await refresh.settle()
    assert len(model.asked) == 1


async def _owner(cid: str) -> uuid.UUID:
    async with engine().begin() as c:
        return (
            await c.execute(text("SELECT owner_id FROM collections WHERE id = :c"), {"c": cid})
        ).scalar_one()


async def test_with_the_model_down_the_first_sources_title_names_it(
    client: AsyncClient,
) -> None:
    cid = (await client.post("/api/collections", json={})).json()["id"]
    await _add(client, cid, NOTE, "Polyps and Reefs")
    assert await _title(client, cid) == "Polyps and Reefs"
    # A source removed names it again, from what is left.
    second = await _add(client, cid, "A second note about sea grass meadows.", "Sea Grass")
    assert await _title(client, cid) == "Polyps and Reefs", "the first by name is the same"
    names = [s["name"] for s in (await client.get(f"/api/collections/{cid}/sources")).json()]
    gone = next(n for n in names if n != second)
    assert (await client.delete(f"/api/collections/{cid}/sources/{gone}")).status_code == 204
    await refresh.settle()
    assert await _title(client, cid) == "Sea Grass"


async def test_a_persons_title_is_never_replaced(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModel("Coral Reef Ecology")
    monkeypatch.setattr(naming, "ai", model)
    cid = (await client.post("/api/collections", json={"title": "My Reefs"})).json()["id"]
    await _add(client, cid, NOTE)
    assert await _title(client, cid) == "My Reefs"
    assert model.asked == []
    # Handed back to the studio, it is named again.
    await client.patch(f"/api/collections/{cid}", json={"title": ""})
    await refresh.settle()
    assert await _title(client, cid) == "Coral Reef Ecology"


async def test_with_naming_off_a_collection_stays_untitled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = await client.patch(f"/api/settings/{st.AUTO_NAME_KEY}", json={"value": "off"})
    assert r.status_code == 200, r.text
    model = FakeModel("Coral Reef Ecology")
    monkeypatch.setattr(naming, "ai", model)
    cid = (await client.post("/api/collections", json={})).json()["id"]
    await _add(client, cid, NOTE, "Polyps")
    assert await _title(client, cid) == ""
    assert model.asked == []


async def test_an_unusable_name_falls_back_to_the_heuristic(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        naming,
        "ai",
        FakeModel(
            "These sources together discuss a number of different things about reefs and "
            "the sea and more"
        ),
    )
    monkeypatch.setattr(covers, "ai", FakeModel(503))
    cid = (await client.post("/api/collections", json={})).json()["id"]
    await _add(client, cid, NOTE, "Polyps")
    assert await _title(client, cid) == "Polyps"
