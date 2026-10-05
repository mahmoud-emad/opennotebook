"""Trying a failed output again: one step on the server, with the options it
was made with, that either starts the new output and removes the failed one,
or changes nothing."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine
from tests.builds.fake import install
from tests.conftest import other_person
from tests.model import add_note


async def _collection(client: AsyncClient) -> str:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    await add_note(client, cid, "Coral reefs are built by polyps over centuries. " * 10)
    return cid


async def _fail(sid: str, **set_: Any) -> None:
    """Mark an output and its job failed, as a build that stopped would; `set_`
    gives JSON columns their own values."""
    cols = "".join(f", {k} = CAST(:{k} AS jsonb)" for k in set_)
    async with engine().begin() as c:
        await c.execute(
            text(
                f"UPDATE sessions SET state = 'failed', failure = 'It stopped.'{cols} WHERE id = :s"
            ),
            {"s": sid, **set_},
        )
        await c.execute(
            text("UPDATE jobs SET status = 'failed', error = 'It stopped.' WHERE session_id = :s"),
            {"s": sid},
        )


async def _args(sid: str) -> dict[str, Any]:
    """What the new output's build job was queued with."""
    async with engine().begin() as c:
        row = (
            await c.execute(
                text(
                    "SELECT p.args FROM jobs j JOIN procrastinate_jobs p "
                    "ON p.id = j.procrastinate_job_id WHERE j.session_id = :s"
                ),
                {"s": sid},
            )
        ).scalar_one()
    return row


async def test_a_failed_deck_is_made_again_with_its_own_options(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, tmp_path)
    cid = await _collection(client)
    first = (
        await client.post(
            f"/api/collections/{cid}/outputs",
            json={"kind": "slides", "style": "clay", "speakers": 2, "title": "Polyps"},
        )
    ).json()
    await _fail(first["id"], slides='[{"title": "a"}, {"title": "b"}, {"title": "c"}]')

    r = await client.post(f"/api/sessions/{first['id']}/retry")
    assert r.status_code == 202, r.text
    new = r.json()
    assert new["id"] != first["id"] and new["state"] == "preparing"
    assert (new["title"], new["kind"], new["speakers"]) == ("Polyps", "slides", 2)
    assert new["audio_label"] == "", "a deck has no audio format"
    args = await _args(new["id"])
    assert (args["style"], args["slide_count"], len(args["speakers"])) == ("clay", 3, 2)
    # The failed one is gone; the new one is in its place.
    listed = (await client.get(f"/api/collections/{cid}")).json()["outputs"]
    assert [o["id"] for o in listed] == [new["id"]]


async def test_a_failed_audio_overview_keeps_its_format_length_and_focus(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, tmp_path)
    cid = await _collection(client)
    first = (
        await client.post(
            f"/api/collections/{cid}/outputs",
            json={
                "kind": "audio",
                "audio_format": "critique",
                "audio_length": "shorter",
                "focus": "the polyps",
            },
        )
    ).json()
    await _fail(first["id"])
    new = (await client.post(f"/api/sessions/{first['id']}/retry")).json()
    assert new["kind"] == "audio" and new["audio_format"] == "critique"
    assert (first["audio_label"], new["audio_label"]) == ("Critique", "Critique")
    args = await _args(new["id"])
    assert (args["audio_format"], args["audio_length"], args["focus"]) == (
        "critique",
        "shorter",
        "the polyps",
    )


async def test_a_refused_retry_keeps_the_failed_output_and_its_reason(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, tmp_path)
    cid = await _collection(client)
    first = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()
    await _fail(first["id"])
    # Over the spending limit: refused, with nothing written.
    monkeypatch.setenv("OPENNOTEBOOK_MAX_BUILD_USD", "0.0000001")
    r = await client.post(f"/api/sessions/{first['id']}/retry")
    assert r.status_code == 422
    assert "over your $0.00 limit" in r.json()["detail"]
    listed = (await client.get(f"/api/collections/{cid}")).json()["outputs"]
    assert [(o["id"], o["state"], o["failure"]) for o in listed] == [
        (first["id"], "failed", "It stopped.")
    ]


async def test_only_a_failed_output_of_ones_own_is_retried(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, tmp_path)
    cid = await _collection(client)
    first = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()
    # Still being made: nothing to try again.
    r = await client.post(f"/api/sessions/{first['id']}/retry")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Only an output that failed can be tried again.")
    await _fail(first["id"])
    them = await other_person(client, "them@test")
    assert (
        await client.post(f"/api/sessions/{first['id']}/retry", headers=them)
    ).status_code == 404
    assert (await client.post(f"/api/sessions/{uuid.uuid4()}/retry")).status_code == 404
