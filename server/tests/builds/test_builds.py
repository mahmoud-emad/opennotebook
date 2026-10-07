"""The build routes end to end: estimate, limit, read-only and owner
refusals, a deck and an audio overview built by the worker against stand-in
model and speech servers, failures in sentences, deletions that stay
deleted, the event stream, and deep research as a job. Plan §9: `ready` is
written once, after validation; a deleted output or collection is never
resurrected; the limit is checked against the high estimate."""

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.db.session import engine
from opennotebook.domain import sessions_estimate as est
from opennotebook.domain import sessions_events, sources
from opennotebook.jobs import Progress
from tests.builds.conftest import drain
from tests.builds.fake import install
from tests.conftest import other_person
from tests.model import add_note

ESTIMATE_KEYS = {
    "limited_usd",
    "total_low_usd",
    "total_typical_usd",
    "total_high_usd",
    "lines",
    "assumptions",
    "sources",
    "source_chars",
    "slides",
    "speakers",
    "style",
    "slides_tier",
    "priced_at",
    "minutes",
    "limit_usd",
    "over_limit",
    "limit_note",
    "model",
    "facts",
}
LINE_KEYS = {
    "group",
    "step",
    "detail",
    "model",
    "via",
    "calls_low",
    "calls_typical",
    "calls_high",
    "input_tokens",
    "output_tokens_low",
    "output_tokens_typical",
    "output_tokens_high",
    "cost_low_usd",
    "cost_typical_usd",
    "cost_high_usd",
    "free",
    "unpriced",
    "price_in_per_million",
    "price_out_per_million",
}


async def _collection(client: AsyncClient, notes: int = 2) -> str:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    for i in range(notes):
        await add_note(
            client, cid, f"Coral reefs, part {i}. Reefs are built by polyps over centuries. " * 8
        )
    return cid


async def _rows(sql: str, **params: Any) -> list[dict[str, Any]]:
    async with engine().begin() as c:
        return [dict(r) for r in (await c.execute(text(sql), params)).mappings()]


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return tmp_path


async def test_the_estimate_is_the_shape_the_web_app_reads(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/outputs/estimate", json={"kind": "slides"})
    assert r.status_code == 200, r.text
    e = r.json()
    assert set(e) == ESTIMATE_KEYS
    assert all(set(ln) == LINE_KEYS for ln in e["lines"])
    assert (e["sources"], e["slides"], e["style"], e["minutes"]) == (2, 5, "editorial", 5)
    assert e["speakers"] == 2, "auto with two sources is two voices"
    assert e["slides_tier"] == "anthropic/claude-haiku-4.5"
    assert e["total_low_usd"] <= e["total_typical_usd"] <= e["total_high_usd"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", e["priced_at"])
    # The default limit is $0.50, and a small deck is well under it.
    assert (e["limit_usd"], e["over_limit"], e["limit_note"]) == (0.5, False, None)
    assert any(ln["group"] == "Designing the slides" for ln in e["lines"])
    priced = next(ln for ln in e["lines"] if ln["step"] == "Outline")
    assert priced["price_in_per_million"] == pytest.approx(1.0)
    # What it is made of, worded by the server for the cost dialog.
    assert e["model"] == "anthropic/claude-haiku-4.5"
    assert e["facts"][:3] == ["5 slides", "about 5 minutes", "2 voices"]
    assert re.fullmatch(r"2 sources · [\d.k]+ characters", e["facts"][3])
    assert e["facts"][4:] == ["Editorial style", "slides by Claude Haiku 4.5"]

    a = (
        await client.post(
            f"/api/collections/{cid}/outputs/estimate",
            json={"kind": "audio", "audio_format": "debate"},
        )
    ).json()
    assert (a["slides"], a["speakers"], a["minutes"], a["style"]) == (4, 2, 8, "")
    assert a["facts"][:3] == ["Debate", "about 8 minutes", "2 voices"]
    assert len(a["facts"]) == 4, "an audio overview has no style and no slide model"
    shorter = (
        await client.post(
            f"/api/collections/{cid}/outputs/estimate",
            json={"kind": "audio", "audio_format": "deep_dive", "audio_length": "shorter"},
        )
    ).json()
    assert shorter["facts"][0] == "Deep Dive · shorter"
    assert not any(ln["group"] == "Designing the slides" for ln in a["lines"])


async def test_an_estimate_with_nothing_to_read_says_what_to_do(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = (await client.post("/api/collections", json={"title": "Empty"})).json()["id"]
    for path in ("outputs/estimate", "outputs"):
        r = await client.post(f"/api/collections/{cid}/{path}", json={"kind": "slides"})
        assert r.status_code == 422
        assert r.json()["detail"] == "Add a source first: a link, a note, or a topic to research."
    r = await client.post(
        f"/api/collections/{cid}/outputs", json={"kind": "slides", "style": "nope"}
    )
    assert r.status_code == 422


async def test_a_build_over_the_limit_is_refused_and_leaves_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    # Lower than any choice the page offers, so a small deck is over it.
    owner = (await client.get("/api/me")).json()["id"]
    await _rows(
        "INSERT INTO user_settings (owner_id, key, value) "
        "VALUES (:o, 'OPENNOTEBOOK_MAX_BUILD_USD', '0.01') RETURNING key",
        o=owner,
    )
    e = (
        await client.post(f"/api/collections/{cid}/outputs/estimate", json={"kind": "slides"})
    ).json()
    assert e["over_limit"] and e["limit_usd"] == 0.01
    # Said before the click as the refusal says it, with what to change.
    assert e["limit_note"].startswith("This could cost up to $")
    assert e["limit_note"].endswith(
        "over your $0.01 limit. Use fewer slides, a shorter length, or raise the limit in "
        "Settings › Costs & limits."
    )
    r = await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})
    assert r.status_code == 422
    said = r.json()["detail"]
    # The HIGH end of the estimate is what is checked and said.
    high = -(-int(e["total_high_usd"] * 1e6) // 10_000) / 100
    assert said == (
        f"This could cost up to ${high:.2f}, over your $0.01 limit. Use fewer slides, a "
        "shorter length, or raise the limit in Settings › Costs & limits."
    )
    assert await _rows("SELECT id FROM sessions") == []
    assert await _rows("SELECT id FROM jobs") == []
    # Nothing queued for the build; the source added before it queued its
    # naming and cover, which is not the build's.
    assert await _rows("SELECT id FROM procrastinate_jobs WHERE queue_name <> 'refresh'") == []


async def test_a_video_overview_is_priced_as_its_deck_and_its_render(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)

    async def priced(path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await client.post(f"/api/collections/{cid}/{path}", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    deck = await priced("outputs/estimate", {"kind": "slides", "speakers": 1, "slide_count": 6})
    slides = await priced("videos/estimate", {"style": "slides"})
    assert set(slides) == ESTIMATE_KEYS
    # Slides are put together with no model call: the deck is the cost.
    assert slides["total_typical_usd"] == pytest.approx(deck["total_typical_usd"])
    assert (slides["slides"], slides["speakers"]) == (6, 1)
    assert slides["facts"][-1] == "Slides video"
    board = await priced("videos/estimate", {})
    # The whiteboard's scenes, about six cents a part, on top of the deck.
    added = board["total_typical_usd"] - deck["total_typical_usd"]
    assert added == pytest.approx(6 * est.WHITEBOARD_USD_PER_PART[1])
    scenes = next(ln for ln in board["lines"] if ln["step"] == "Whiteboard scenes")
    assert scenes["model"] == "anthropic/claude-sonnet-5.5"
    assert board["facts"][-2:] == ["Whiteboard video", "Whiteboard theme"]
    short = await priced("videos/estimate", {"length": "short"})
    long = await priced("videos/estimate", {"length": "long"})
    assert (short["slides"], long["slides"]) == (4, 9)
    assert short["total_typical_usd"] < board["total_typical_usd"] < long["total_typical_usd"]
    # A drawn theme costs nothing more; an illustrated one adds its pictures.
    chalk = await priced("videos/estimate", {"theme": "chalkboard"})
    assert chalk["total_high_usd"] == pytest.approx(board["total_high_usd"])
    assert chalk["facts"][-1] == "Chalkboard theme"
    painted = await priced("videos/estimate", {"theme": "watercolor"})
    assert painted["total_typical_usd"] > board["total_typical_usd"]
    # Only the deck is held to the spending limit: the render is not stopped
    # by it, so a whiteboard is not refused for the render's high end.
    assert board["limited_usd"] == pytest.approx(deck["total_high_usd"])
    assert board["limited_usd"] < board["total_high_usd"]
    assert not board["over_limit"]


async def test_a_read_only_copy_is_not_built_from(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    await _rows("UPDATE collections SET read_only = true WHERE id = :c RETURNING id", c=cid)
    for path in ("outputs", "outputs/estimate", "research"):
        body = {"topic": "reefs"} if path == "research" else {"kind": "slides"}
        r = await client.post(f"/api/collections/{cid}/{path}", json=body)
        assert r.status_code == 403
        assert "read-only copy" in r.json()["detail"]
    assert await _rows("SELECT id FROM jobs") == []


async def test_one_persons_outputs_are_not_anothers(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    job = (await _rows("SELECT id FROM jobs"))[0]["id"]
    them = await other_person(client, "them@example.com")
    for method, path, body in [
        ("POST", f"/api/collections/{cid}/outputs", {"kind": "slides"}),
        ("POST", f"/api/collections/{cid}/outputs/estimate", {"kind": "slides"}),
        ("POST", f"/api/collections/{cid}/research", {"topic": "reefs"}),
        ("GET", f"/api/sessions/{sid}", None),
        ("GET", f"/api/sessions/{sid}/events", None),
        ("GET", f"/api/jobs/{job}", None),
        ("DELETE", f"/api/sessions/{sid}", None),
    ]:
        r = await client.request(method, path, json=body, headers=them)
        assert r.status_code == 404, (path, r.text)
        assert r.json()["detail"].endswith("Reload the page to see what is.")
    assert (await client.get(f"/api/sessions/{sid}")).json()["state"] == "preparing"


async def test_a_deck_is_built_voiced_checked_and_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, voice = install(monkeypatch, files)
    cid = await _collection(client)
    sid = (
        await client.post(
            f"/api/collections/{cid}/outputs", json={"kind": "slides", "slide_count": 3}
        )
    ).json()["id"]
    await drain("prep")
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["state"] == "ready", got["failure"]
    assert got["failure"] is None and got["parts"] == 3
    assert got["duration_ms"] > 0 and got["speakers"] == 2
    assert [x["display_name"] for x in got["speaker_list"]] == ["Bella", "Adam"]
    assert got["spent_known"] and float(got["spent_usd"]) > 0
    deck = await _rows("SELECT deck_ref FROM sessions WHERE id = :s", s=sid)
    assert deck[0]["deck_ref"] == {"collection": sid, "presentation": "studio"}
    for part in got["slides"]:
        ref = part["slide_ref"]
        assert (ref["collection"], ref["presentation"]) == (sid, "studio")
        html = (files / "decks" / sid / "studio" / f"{ref['slide']}.html").read_text()
        assert "data-kit" in html
        for line in part["lines"]:
            assert line["audio_path"] == f"audio/{sid}/{line['line_id']}.wav"
            assert (files / line["audio_path"]).is_file() and line["duration_ms"] > 0
    assert len(voice.said) == sum(len(p["lines"]) for p in got["slides"])
    assert "slides" in studio.asked and "qa" in studio.asked
    # The ledger: every model call of the build, against the output and job.
    job = (await _rows("SELECT * FROM jobs WHERE session_id = :s", s=sid))[0]
    assert (job["status"], job["steps_done"], job["steps_total"]) == ("done", 5, 5)
    spent = await _rows(
        "SELECT kind, job_id, session_id FROM usage_events WHERE session_id = :s", s=sid
    )
    assert len(spent) == len([k for k in studio.asked])
    assert {r["job_id"] for r in spent} == {job["id"]}
    assert {r["kind"] for r in spent} >= {"qa_extract", "script", "slides"}
    # Extraction ran once per source: a second build asks no Q&A again.
    asked = studio.asked.count("qa")
    await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides", "slide_count": 3})
    await drain("prep")
    assert studio.asked.count("qa") == asked


async def test_an_audio_overview_is_one_host_in_two_chapters_and_no_deck(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    cid = await _collection(client)
    r = await client.post(
        f"/api/collections/{cid}/outputs",
        json={"kind": "audio", "audio_format": "brief", "focus": "bleaching"},
    )
    sid = r.json()["id"]
    assert r.json()["title"] == "Brief audio overview · bleaching"
    await drain("prep")
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["state"] == "ready", got["failure"]
    assert (got["kind"], got["parts"], got["speakers"], got["audio_format"]) == (
        "audio",
        2,
        1,
        "brief",
    )
    assert got["audio"]["minutes"] == 2 and got["style"] is None
    assert not (files / "decks" / sid).exists()
    assert "slides" not in studio.asked
    # The episode the player downloads joins every line.
    assert (await client.get(f"/api/sessions/{sid}/episode")).status_code == 200


async def test_a_failed_build_says_why_and_what_it_cost(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    studio.fail["outline"] = 402
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    await drain("prep")
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["state"] == "failed"
    assert got["failure"].startswith("The AI account is out of credit")
    # The extraction before the failure was paid for, and says so.
    assert float(got["spent_usd"]) > 0
    job = (await _rows("SELECT status, error FROM jobs WHERE session_id = :s", s=sid))[0]
    assert job["status"] == "failed" and job["error"]


async def test_ready_is_never_written_when_the_check_fails(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    _, voice = install(monkeypatch, files)
    voice.status = 500
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    states: list[str] = []
    stream = sessions_events.stream(
        uuid.UUID((await client.get("/api/me")).json()["id"]), uuid.UUID(sid)
    )

    async def follow() -> None:
        async for e in stream:
            if e is not None and e[0] == "session.state":
                states.append(e[1]["state"])
                if e[1]["state"] in ("ready", "failed"):
                    return

    following = asyncio.create_task(follow())
    await asyncio.sleep(0.2)
    await drain("prep")
    await asyncio.wait_for(following, 20)
    assert states == ["preparing", "failed"]
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert "voice server" in got["failure"]


async def test_an_output_deleted_before_its_build_stays_deleted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 204
    other = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    assert (await client.delete(f"/api/collections/{cid}")).status_code == 204
    await drain("prep")
    assert await _rows("SELECT id FROM sessions") == []
    assert studio.asked == [], "nothing was built for what was deleted"
    assert not (files / "audio" / sid).exists() and not (files / "audio" / other).exists()


async def test_an_output_deleted_while_it_is_made_stays_deleted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    from opennotebook.build import pipeline

    real = pipeline.ingest

    async def ingest_then_delete(owner: uuid.UUID, c: uuid.UUID) -> int:
        n = await real(owner, c)
        # The person deletes it while the script is being written; the stop
        # has not reached this build yet.
        await _rows("DELETE FROM sessions WHERE id = :s RETURNING id", s=sid)
        return n

    monkeypatch.setattr(pipeline, "ingest", ingest_then_delete)
    await drain("prep")
    assert await _rows("SELECT id FROM sessions") == []
    assert "slides" not in studio.asked, "the build stopped at its next write"


async def test_the_event_stream_follows_progress_through_notify(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    owner = uuid.UUID((await client.get("/api/me")).json()["id"])
    job: uuid.UUID = (await _rows("SELECT id FROM jobs"))[0]["id"]
    stream = sessions_events.stream(owner, uuid.UUID(sid))
    first = await asyncio.wait_for(anext(stream), 5)
    assert first == ("session.state", {"state": "preparing"})
    # Queued, with its steps known from the start.
    second = await asyncio.wait_for(anext(stream), 5)
    assert second == (
        "prep.progress",
        {"step": "", "label": "Starting", "steps_done": 0, "steps_total": 5},
    )
    # No worker is running here, and the stream says so.
    third = await asyncio.wait_for(anext(stream), 5)
    assert third is not None and third[0] == "prep.waiting"
    assert third[1]["waiting"].startswith("Waiting for the studio's worker to start.")
    p = Progress(job, 5)
    await p.start()
    await p.phase("ingest")
    # Once the build runs it is no longer waiting, and its step comes through.
    seen = [await asyncio.wait_for(anext(stream), 5) for _ in range(2)]
    assert ("prep.waiting", {"waiting": None}) in seen
    # The step in a person's words, as the player says it.
    assert (
        "prep.progress",
        {"step": "ingest", "label": "Reading your sources", "steps_done": 0, "steps_total": 5},
    ) in seen
    # Playback is announced too.
    r = await client.put(
        f"/api/sessions/{sid}/playback",
        json={"slide_ordinal": 0, "line_id": "", "offset_ms": 0, "state": "paused"},
    )
    assert r.status_code == 200
    got = await asyncio.wait_for(anext(stream), 5)
    assert got == ("session.state", {"state": "paused"})
    await stream.aclose()


async def test_deep_research_is_a_job_that_adds_its_report(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)

    async def page(_: object, url: str) -> sources.Page:
        return sources.Page(f"Page {url[-1]}", "Reefs are built by coral polyps. " * 20)

    monkeypatch.setattr(sources, "read_page", page)
    cid = await _collection(client, notes=0)
    r = await client.post(f"/api/collections/{cid}/research", json={"topic": "coral reefs"})
    assert r.status_code == 202, r.text
    job = r.json()
    assert (job["kind"], job["status"], job["steps_total"]) == ("research", "queued", 1)
    # Queued with no worker running: the job says why it has not started.
    queued = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert queued["waiting"].startswith("Waiting for the studio's worker to start.")
    await drain("work")
    done = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert done["status"] == "done", done
    assert done["waiting"] is None
    listed = (await client.get(f"/api/collections/{cid}/sources")).json()
    assert [(s["kind"], s["title"]) for s in listed] == [("research", "Web research: coral reefs")]
    assert {"research_plan", "search", "report"} <= set(studio.asked)
    spent = await _rows("SELECT kind FROM usage_events WHERE job_id = :j", j=job["id"])
    assert spent and {s["kind"] for s in spent} == {"research"}


async def test_research_that_finds_nothing_says_so(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    studio.fail["search"] = 503
    cid = await _collection(client, notes=0)
    job = (await client.post(f"/api/collections/{cid}/research", json={"topic": "x"})).json()
    await drain("work")
    done = (await client.get(f"/api/jobs/{job['id']}")).json()
    assert done["status"] == "failed"
    assert done["error"] == (
        "The web searches found nothing on that topic. Try wording it differently."
    )


async def test_a_build_with_no_ai_key_is_refused_at_once(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    from opennotebook.ai import client as ai_client
    from opennotebook.ai.client import Ai

    keyless = Ai("http://ai.test/v1", "")
    monkeypatch.setattr(ai_client, "ai", lambda: keyless)
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})
    assert r.status_code == 503
    assert r.json()["detail"] == (
        "The studio has no AI key yet. Add OPENNOTEBOOK_AI_KEY to the server's environment, "
        "then try again."
    )
    assert await _rows("SELECT id FROM sessions") == []


async def test_a_queued_build_says_when_no_worker_is_there_to_start_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["waiting"].startswith("Waiting for the studio's worker to start.")
    outputs = (await client.get(f"/api/collections/{cid}")).json()["outputs"]
    assert outputs[0]["waiting"] == got["waiting"]
    # A worker that reported a moment ago is there to start it.
    await _rows("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
    assert (await client.get(f"/api/sessions/{sid}")).json()["waiting"] is None
