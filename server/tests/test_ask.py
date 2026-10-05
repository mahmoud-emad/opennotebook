"""Ask with citations: ported from opennotebook_script/src/cite.rs, and the
route (`source_ask` in the Rust server) against a mocked model."""

import uuid

import pytest
from httpx import AsyncClient

from opennotebook.script.cite import renumber, user_prompt
from opennotebook.script.mindmap import NamedDoc
from tests.conftest import other_person
from tests.model import add_note, fails, install, says, spent

# ── renumbering ───────────────────────────────────────────────────────────────


def test_markers_are_renumbered_in_order_of_first_use() -> None:
    t, order = renumber("Latency is 200 ms [5]. It streams [2]. Again [5].", 8)
    assert t == "Latency is 200 ms [1]. It streams [2]. Again [1]."
    assert order == [4, 1]


def test_a_number_past_the_passages_is_removed_with_its_space() -> None:
    t, order = renumber("A claim [9]. Another [1].", 8)
    assert t == "A claim. Another [1]."
    assert order == [0]
    t, order = renumber("Zero is not a passage [0].", 8)
    assert t == "Zero is not a passage."
    assert order == []


def test_a_list_marker_becomes_separate_markers_and_drops_its_bad_numbers() -> None:
    t, order = renumber("Both say so [3, 9, 1].", 8)
    assert t == "Both say so [1][2]."
    assert order == [2, 0]
    t, _ = renumber("Twice [2,2].", 8)
    assert t == "Twice [1]."


def test_links_and_ordinary_brackets_are_left_alone() -> None:
    raw = "See [the paper](https://x.org) and [note] and [1](https://y.org) [2]."
    t, order = renumber(raw, 8)
    assert t == "See [the paper](https://x.org) and [note] and [1](https://y.org) [1]."
    assert order == [1]


def test_an_unclosed_bracket_ends_the_text_unchanged() -> None:
    t, order = renumber("Cut off here [3", 8)
    assert t == "Cut off here [3"
    assert order == []


def test_an_uncited_reply_is_kept_whole() -> None:
    t, order = renumber("The sources do not cover this.", 8)
    assert t == "The sources do not cover this."
    assert order == []


def test_passages_are_numbered_with_their_source_title() -> None:
    docs = [NamedDoc("a.md", "Moshi", ""), NamedDoc("b.md", "Mimi", "")]
    p = user_prompt(docs, [(1, "codec"), (0, "latency")], " Why? ")
    assert "[1] (Mimi) codec" in p
    assert "[2] (Moshi) latency" in p
    assert p.endswith("Question: Why?")


# ── the route ─────────────────────────────────────────────────────────────────

REEFS = (
    "Coral reefs cover less than one percent of the ocean floor "
    "but shelter a quarter of marine species."
)
KELP = "Kelp forests grow along cold coasts and shelter fish, otters and sea urchins."


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def test_an_answer_cites_the_passages_it_rests_on(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    reefs = await add_note(client, cid, REEFS)
    kelp = await add_note(client, cid, KELP)
    # Numbered by the passages the model was given: [1] reefs, [2] kelp. [9]
    # names no passage.
    model.answers(says("Kelp shelters otters [2]. Reefs shelter a quarter of species [1][9]."))
    r = await client.post(
        f"/api/collections/{cid}/ask", json={"question": "What do coral reefs shelter?"}
    )
    assert r.status_code == 200, r.text
    got = r.json()
    # Renumbered by first use, the bad number gone.
    assert got["answer"] == "Kelp shelters otters [1]. Reefs shelter a quarter of species [2]."
    assert [(c["n"], c["name"]) for c in got["citations"]] == [(1, kelp), (2, reefs)]
    assert got["citations"][1]["excerpt"] == REEFS
    assert got["citations"][1]["title"] == "Coral reefs cover less than one"

    # The chat model, the passages and the question went to the model.
    assert model.bodies[0]["model"] == "google/gemini-2.5-flash-lite"
    assert "[1] (Coral reefs cover less than one) Coral reefs" in model.said_to(0, "user")
    assert "square brackets" in model.said_to(0, "system")
    (row,) = await spent()
    assert row["kind"] == "ask" and str(row["collection_id"]) == cid
    assert row["priced_by"] == "catalogue"


async def test_only_the_named_sources_are_asked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    kelp = await add_note(client, cid, KELP)
    model.answers(says("Otters [1]."))
    r = await client.post(
        f"/api/collections/{cid}/ask", json={"question": "What lives there?", "sources": [kelp]}
    )
    assert r.status_code == 200, r.text
    assert "Coral" not in model.said_to(0, "user")
    assert r.json()["citations"][0]["name"] == kelp


async def test_a_language_other_than_english_is_asked_for(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    r = await client.patch("/api/settings/OPENNOTEBOOK_LANGUAGE", json={"value": "French"})
    assert r.status_code == 200, r.text
    model.answers(says("Les récifs [1]."))
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "Reefs?"})
    assert r.status_code == 200, r.text
    assert "Write everything you say in French" in model.said_to(0, "system")


async def test_an_empty_collection_is_told_to_add_a_source(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "Anything?"})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Add a source first")


async def test_a_source_that_is_not_there_is_named(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    r = await client.post(
        f"/api/collections/{cid}/ask", json={"question": "Reefs?", "sources": ["gone.md"]}
    )
    assert r.status_code == 422
    assert "“gone.md” is not a source of this collection" in r.json()["detail"]


async def test_a_blank_question_is_refused(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "   "})
    assert r.status_code == 422
    assert r.json()["detail"] == "The question is empty. Write a question, then ask again."


async def test_an_account_out_of_credit_is_a_sentence(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(fails(402, "Insufficient credits"))
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "Reefs?"})
    assert r.status_code == 402
    assert r.json()["detail"].startswith("The AI account is out of credit")


async def test_a_cut_off_answer_is_not_passed_off_as_whole(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(says("Reefs shelter a quarter of [1", finish="length"))
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "Reefs?"})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The AI model ran out of room before it finished")
    # Billed all the same.
    assert len(await spent()) == 1


async def test_nobody_else_can_ask_a_collection(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    them = await other_person(client, "them@test")
    r = await client.post(f"/api/collections/{cid}/ask", json={"question": "Reefs?"}, headers=them)
    assert r.status_code == 404
    r = await client.post(f"/api/collections/{uuid.uuid4()}/ask", json={"question": "Reefs?"})
    assert r.status_code == 404
