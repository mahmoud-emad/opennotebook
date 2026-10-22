"""Transformations: the built-in and a person's own, run over the sources."""

import pytest
from httpx import AsyncClient

from opennotebook.domain import transformations as tf
from opennotebook.script import transform
from opennotebook.script.mindmap import NamedDoc
from tests.conftest import other_person
from tests.model import add_note, fails, install, says, spent

REEFS = (
    "Coral reefs cover less than one percent of the ocean floor "
    "but shelter a quarter of marine species."
)
KELP = "Kelp forests grow along cold coasts and shelter fish, otters and sea urchins."


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


# ── choosing what the model reads ────────────────────────────────────────────


def test_passages_that_fit_are_all_given_whatever_the_focus() -> None:
    docs = [NamedDoc("a.md", "A", REEFS), NamedDoc("b.md", "B", KELP)]
    assert [(i, p) for i, p in transform.choose(docs, "")] == [(0, REEFS), (1, KELP)]
    assert len(transform.choose(docs, "otters")) == 2


def test_passages_over_the_budget_are_an_even_spread_or_the_ones_about_the_focus() -> None:
    para = "word " * 150  # 750 characters: one passage each
    texts = "\n\n".join(f"{para}{'otters' if i == 90 else f'x{i}'}" for i in range(100))
    docs = [NamedDoc("a.md", "A", texts)]
    spread = transform.choose(docs, "")
    assert 0 < len(spread) <= transform.BUDGET_CHARS // 900
    assert sum(len(p) for _, p in spread) <= transform.BUDGET_CHARS
    # Reaching across the whole source, not just its start.
    assert any("x9" in p for _, p in spread[-3:])
    focused = transform.choose(docs, "otters")
    assert any("otters" in p for _, p in focused)


def test_a_name_is_found_at_the_start_of_what_follows_the_command() -> None:
    every = [*tf.BUILTINS, tf.Found("id-1", "Key claims about reefs", "", "p", False)]
    assert tf.split(every, "summary")[0] == tf.BUILTINS[0]
    f, focus = tf.split(every, "Timeline of the 1990s")
    assert f is not None and f.id == "timeline" and focus == "of the 1990s"
    # The longest name wins, and the key works as the name does.
    f, focus = tf.split(every, "key claims about reefs and kelp")
    assert f is not None and f.id == "id-1" and focus == "and kelp"
    f, focus = tf.split(every, "key-claims coral")
    assert f is not None and f.id == "key-claims" and focus == "coral"
    assert tf.split(every, "nonsense here") == (None, "nonsense here")


# ── a person's own ───────────────────────────────────────────────────────────


async def test_the_built_in_come_first_and_a_person_adds_changes_and_removes_their_own(
    client: AsyncClient,
) -> None:
    listed = (await client.get("/api/transformations")).json()
    assert [t["id"] for t in listed] == [b.id for b in tf.BUILTINS]
    assert all(t["builtin"] for t in listed)

    r = await client.post(
        "/api/transformations", json={"name": "  Glossary ", "prompt": " Define the terms. "}
    )
    assert r.status_code == 201, r.text
    mine = r.json()
    assert mine["name"] == "Glossary" and mine["prompt"] == "Define the terms."
    assert mine["builtin"] is False

    r = await client.put(
        f"/api/transformations/{mine['id']}",
        json={"name": "Glossary", "prompt": "Define every term."},
    )
    assert r.status_code == 200 and r.json()["prompt"] == "Define every term."
    listed = (await client.get("/api/transformations")).json()
    assert [t["name"] for t in listed][-1] == "Glossary" and len(listed) == len(tf.BUILTINS) + 1

    assert (await client.delete(f"/api/transformations/{mine['id']}")).status_code == 204
    r = await client.delete(f"/api/transformations/{mine['id']}")
    assert r.status_code == 404 and r.json()["detail"].startswith("That transformation")


async def test_a_bad_or_taken_name_and_the_built_in_are_refused_in_words(
    client: AsyncClient,
) -> None:
    r = await client.post("/api/transformations", json={"name": " ", "prompt": "x"})
    assert r.status_code == 422 and r.json()["detail"].startswith("The transformation has no name")
    r = await client.post("/api/transformations", json={"name": "A", "prompt": "  "})
    assert r.status_code == 422 and r.json()["detail"].startswith("The instruction is empty")
    r = await client.post("/api/transformations", json={"name": "x" * 61, "prompt": "p"})
    assert r.status_code == 422 and "too long" in r.json()["detail"]
    r = await client.post("/api/transformations", json={"name": "summary", "prompt": "p"})
    assert r.status_code == 422 and "built-in" in r.json()["detail"]

    ok = {"name": "Glossary", "prompt": "Define."}
    assert (await client.post("/api/transformations", json=ok)).status_code == 201
    r = await client.post("/api/transformations", json=ok)
    assert (
        r.status_code == 409
        and "already have a transformation called “Glossary”" in r.json()["detail"]
    )

    r = await client.delete("/api/transformations/summary")
    assert r.status_code == 422 and "built in" in r.json()["detail"]
    r = await client.put("/api/transformations/timeline", json=ok)
    assert r.status_code == 422 and "built in" in r.json()["detail"]


async def test_nobody_else_can_change_or_see_a_persons_own(client: AsyncClient) -> None:
    mine = (
        await client.post("/api/transformations", json={"name": "Glossary", "prompt": "Define."})
    ).json()
    them = await other_person(client, "other@example.org")
    assert [t["name"] for t in (await client.get("/api/transformations", headers=them)).json()] == [
        b.name for b in tf.BUILTINS
    ]
    r = await client.delete(f"/api/transformations/{mine['id']}", headers=them)
    assert r.status_code == 404
    r = await client.put(
        f"/api/transformations/{mine['id']}", json={"name": "G", "prompt": "p"}, headers=them
    )
    assert r.status_code == 404
    # And the same name is theirs to take.
    r = await client.post(
        "/api/transformations", json={"name": "Glossary", "prompt": "Define."}, headers=them
    )
    assert r.status_code == 201


# ── running one ──────────────────────────────────────────────────────────────


async def test_a_run_cites_the_passages_and_keeps_the_result_as_a_note(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    reefs = await add_note(client, cid, REEFS, "Reefs")
    await add_note(client, cid, KELP, "Kelp")
    # Passages are numbered in reading order: [1] reefs, [2] kelp. [9] names none.
    model.answers(says("Kelp shelters otters [2]. Reefs shelter a quarter of species [1][9]."))
    r = await client.post(
        f"/api/collections/{cid}/transform",
        json={"transformation": "summary", "focus": "shelter"},
    )
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["title"] == "Summary: shelter"
    assert got["text"] == "Kelp shelters otters [1]. Reefs shelter a quarter of species [2]."
    assert [(c["n"], c["title"]) for c in got["citations"]] == [(1, "Kelp"), (2, "Reefs")]
    assert got["citations"][1]["name"] == reefs

    # What the model was told: the instruction, the focus and both passages.
    said = model.said_to(0, "user")
    assert "Instruction: Summarize the sources" in said and "Focus on: shelter" in said
    assert "[1] (Reefs)" in said and "[2] (Kelp)" in said
    assert "square brackets" in model.said_to(0, "system")

    # Kept as a note: the result, then the passages it cites.
    note = got["source"]
    assert note["kind"] == "text" and note["title"] == "Summary: shelter"
    body = (await client.get(f"/api/collections/{cid}/sources/{note['name']}")).json()["text"]
    assert "Kelp shelters otters [1]." in body
    assert "Sources:\n\n1. Kelp\n2. Reefs" in body
    (row,) = await spent()
    assert row["kind"] == "transform" and str(row["collection_id"]) == cid


async def test_a_run_not_to_be_saved_adds_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS, "Reefs")
    model.answers(says("Reefs shelter species [1]."))
    r = await client.post(
        f"/api/collections/{cid}/transform", json={"transformation": "Key Claims", "save": False}
    )
    assert r.status_code == 200 and r.json()["source"] is None
    assert len((await client.get(f"/api/collections/{cid}/sources")).json()) == 1


async def test_a_persons_own_runs_by_id_and_a_source_that_is_off_is_left_out(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS, "Reefs")
    kelp = await add_note(client, cid, KELP, "Kelp")
    reefs = (await client.get(f"/api/collections/{cid}/sources")).json()[0]["name"]
    await client.patch(f"/api/collections/{cid}/sources/{reefs}", json={"active": False})
    mine = (
        await client.post("/api/transformations", json={"name": "Glossary", "prompt": "Define."})
    ).json()
    model.answers(says("Kelp: a seaweed forest [1]."))
    r = await client.post(
        f"/api/collections/{cid}/transform", json={"transformation": mine["id"], "save": False}
    )
    assert r.status_code == 200, r.text
    assert "Instruction: Define." in model.said_to(0, "user")
    assert "Coral" not in model.said_to(0, "user")
    assert r.json()["citations"][0]["name"] == kelp


async def test_an_unknown_name_lists_what_there_is(client: AsyncClient) -> None:
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    r = await client.post(f"/api/collections/{cid}/transform", json={"transformation": "poem"})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("There is no transformation called “poem”. The ones")
    assert "Summary, Key claims" in r.json()["detail"]


async def test_an_empty_collection_and_a_stranger_and_a_failing_model(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/transform", json={"transformation": "summary"})
    assert r.status_code == 409

    await add_note(client, cid, REEFS)
    them = await other_person(client, "other@example.org")
    r = await client.post(
        f"/api/collections/{cid}/transform", json={"transformation": "summary"}, headers=them
    )
    assert r.status_code == 404

    model.answers(fails(402, "Insufficient credits"))
    r = await client.post(f"/api/collections/{cid}/transform", json={"transformation": "summary"})
    assert r.status_code >= 400 and r.json()["detail"]
    assert len((await client.get(f"/api/collections/{cid}/sources")).json()) == 1
