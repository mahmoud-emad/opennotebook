"""The deprecated JSON-RPC adapter (api/rpc.py): the framing ported from
`crates/opennotebook_api/src/tests.rs`, and each of the 47 old methods
against the real domain, with the model and the build's services stood in.
Delete with the adapter."""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import research, storage
from opennotebook.api import rpc_methods
from opennotebook.api.rpc_methods import METHODS
from opennotebook.config import settings as config
from opennotebook.db.session import engine
from opennotebook.domain import settings as st
from opennotebook.jobs.events import hub
from tests.builds.fake import install as install_build
from tests.conftest import other_person
from tests.model import add_note, fails, install, says
from tests.test_ingest import PAGE
from tests.test_mindmap import OUTLINE, REEFS
from tests.test_notes import MIMI, MOSHI, NOTES
from tests.web import fake_web

OSCHEMA = Path(__file__).parents[2] / "crates" / "opennotebook_api" / "oschema"


@pytest.fixture(autouse=True)
async def empty_queue() -> AsyncIterator[None]:
    yield
    await hub.close()
    async with engine().begin() as c:
        await c.execute(text("TRUNCATE procrastinate_jobs, procrastinate_workers CASCADE"))


async def call(
    client: AsyncClient,
    domain: str,
    method: str,
    params: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One request with id 1, and its whole answer."""
    req: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    r = await client.post(f"/api/{domain}/rpc", json=req, headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["deprecation"] == "true"
    out: dict[str, Any] = r.json()
    assert out["id"] == 1 and out["jsonrpc"] == "2.0"
    return out


async def ok(client: AsyncClient, domain: str, method: str, params: Any = None) -> Any:
    out = await call(client, domain, method, params)
    assert "result" in out, out
    return out["result"]


async def err(
    client: AsyncClient,
    domain: str,
    method: str,
    params: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    out = await call(client, domain, method, params, headers)
    assert "error" in out, out
    e: dict[str, Any] = out["error"]
    # Every message is a sentence a person can act on.
    assert e["message"][0].isupper() and e["message"].endswith((".", "…")), e
    return e


async def _rows(sql: str, **params: Any) -> list[dict[str, Any]]:
    async with engine().begin() as c:
        return [dict(r) for r in (await c.execute(text(sql), params)).mappings()]


async def _cid(client: AsyncClient, title: str = "Reefs") -> str:
    made = await ok(client, "session", "collection_create", {"req": {"title": title}})
    return made["cid"]


async def _output(client: AsyncClient, cid: str, state: str = "ready") -> str:
    """A finished deck, written as a build writes one."""
    owner = (await client.get("/api/me")).json()["id"]
    slides = [
        {
            "slide_ref": {"collection": "c", "presentation": "p", "slide": "polyps"},
            "ordinal": 0,
            "aspect": {"width": 1920, "height": 1080},
            "title": "Polyps",
            "on_slide": ["Polyps"],
            "lines": [
                {
                    "line_id": "l1",
                    "speaker_id": "host",
                    "ordinal": 0,
                    "text": "Reefs are built by polyps.",
                    "audio_path": "audio/l1.wav",
                    "duration_ms": 1500,
                    "cues": [],
                },
                {"line_id": "l2", "speaker_id": "host", "ordinal": 1, "text": "Slowly."},
            ],
        }
    ]
    speakers = [{"speaker_id": "host", "voice_id": "af_bella", "display_name": "Bella"}]
    (row,) = await _rows(
        "INSERT INTO sessions (owner_id, collection_id, kind, title, state, slides, speakers,"
        " style, duration_ms, description, spent_usd, spent_known)"
        " VALUES (:o, :c, 'slides', 'Deck', :state, CAST(:slides AS jsonb),"
        " CAST(:speakers AS jsonb), 'editorial', 1500, 'Reefs are built by polyps.', 0.02, true)"
        " RETURNING id",
        o=owner,
        c=cid,
        state=state,
        slides=json.dumps(slides),
        speakers=json.dumps(speakers),
    )
    return str(row["id"])


# ── framing ──────────────────────────────────────────────────────────────────


async def test_a_call_is_answered_by_name_with_named_or_positional_params(
    client: AsyncClient,
) -> None:
    cid = await _cid(client)
    got = await ok(client, "sources", "source_list", {"sid": cid})
    assert got == {"sources": []}
    assert await ok(client, "sources", "source_list", [cid]) == {"sources": []}
    r = await client.post(
        "/api/notes/rpc", json={"jsonrpc": "2.0", "id": "a", "method": "notes_list_all"}
    )
    assert r.json() == {"jsonrpc": "2.0", "id": "a", "result": {"notes": []}}


async def test_a_method_with_no_params_takes_none(client: AsyncClient) -> None:
    for params in (None, {}):
        assert await ok(client, "notes", "notes_list_all", params) == {"notes": []}
    r = await client.post(
        "/api/notes/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "notes_list_all", "params": None},
    )
    assert r.json()["result"] == {"notes": []}


async def test_errors_carry_codes_and_sentences(client: AsyncClient) -> None:
    e = await err(client, "notes", "nope")
    assert e["code"] == -32601 and "'nope'" in e["message"] and "rpc.discover" in e["message"]
    e = await err(client, "notes", "mindmap_list_all")
    assert e["code"] == -32601 and e["message"].endswith("It is served at /api/mindmap/rpc.")

    e = await err(client, "notes", "notes_get", {"req": 5})
    assert e["code"] == -32602 and e["data"] == {"status": 422}
    e = await err(client, "notes", "notes_get", {})
    assert e == {"code": -32602, "message": "Fill in req, then try again.", "data": {"status": 422}}
    e = await err(client, "notes", "notes_get", "sid")
    assert e["code"] == -32602 and e["message"].startswith("Params must be an object")

    r = await client.post("/api/notes/rpc", json={"jsonrpc": "1.0", "id": 1, "method": "x"})
    assert r.json()["error"]["code"] == -32600 and r.json()["id"] == 1
    r = await client.post("/api/notes/rpc", json={"jsonrpc": "2.0", "id": 1})
    assert r.json()["error"]["code"] == -32600 and r.json()["id"] is None
    r = await client.post("/api/notes/rpc", content=b"{not json")
    assert r.status_code == 200 and r.json()["error"]["code"] == -32700
    assert r.json()["error"]["message"].startswith("The request is not valid JSON")
    for batch in ([], [{"jsonrpc": "2.0", "id": 1, "method": "rpc.health"}] * 101):
        r = await client.post("/api/notes/rpc", json=batch)
        assert r.json()["error"]["code"] == -32600
        assert r.json()["error"]["message"].startswith("A batch holds 1 to 100 requests")


async def test_notifications_get_nothing_and_batches_get_each_answer(
    client: AsyncClient,
) -> None:
    r = await client.post("/api/notes/rpc", json={"jsonrpc": "2.0", "method": "notes_list_all"})
    assert r.status_code == 204 and r.content == b"" and r.headers["deprecation"] == "true"
    # A notification still runs.
    r = await client.post(
        "/api/sources/rpc", json={"jsonrpc": "2.0", "id": None, "method": "draft_create"}
    )
    assert r.status_code == 204
    assert len(await _rows("SELECT id FROM collections")) == 1

    r = await client.post(
        "/api/notes/rpc",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "notes_list_all"},
            {"jsonrpc": "2.0", "method": "notes_list_all"},
            {"jsonrpc": "2.0", "id": 2, "method": "rpc.health"},
            {"jsonrpc": "2.0", "id": 3, "method": "notes_get", "params": {"req": {}}},
        ],
    )
    got = r.json()
    assert [a["id"] for a in got] == [1, 2, 3]
    assert got[1]["result"] == {"status": "ok", "service": "NotesService", "version": "0.1.0"}
    # One failed call in a batch is that call's answer alone.
    assert got[2]["error"]["code"] == -32602 and got[0]["result"] == {"notes": []}


def _oschema_methods(domain: str) -> list[str]:
    """The method names of a domain's service block, in order."""
    names: list[str] = []
    inside = False
    for line in (OSCHEMA / domain / f"{domain}.oschema").read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if s.startswith("service "):
            inside = True
        elif inside and "(" in s and "->" in s:
            names.append(s.split("(", 1)[0])
    return names


async def test_discover_lists_all_47_methods_and_each_is_served(client: AsyncClient) -> None:
    total = 0
    for domain, methods in METHODS.items():
        doc = await ok(client, domain, "rpc.discover")
        names = [m["name"] for m in doc["methods"]]
        if OSCHEMA.exists():
            assert names == _oschema_methods(domain), domain
        assert sorted(names) == sorted(methods), domain
        total += len(names)
        r = await client.get(f"/api/{domain}/openrpc.json")
        assert r.json() == doc
    assert total == 47
    notes = await ok(client, "notes", "rpc.discover")
    assert "citations" in notes["components"]["schemas"]["StudyNotes"]["required"]
    assert notes["methods"][0]["params"][0]["schema"]["$ref"] == (
        "#/components/schemas/NotesCreateReq"
    )
    listed = (await client.get("/api/domains.json")).json()
    assert sum(len(d["methods"]) for d in listed) == 47
    # Not part of the REST contract the web app's client is generated from.
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert not [p for p in paths if p.endswith(("/rpc", "openrpc.json", "domains.json"))]


async def test_a_caller_signs_in_as_rest_does(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    them = await other_person(client, "them@example.com")
    monkeypatch.setattr(config(), "auth", "keys")
    req = {"jsonrpc": "2.0", "id": 1, "method": "collection_list"}
    r = await client.post("/api/session/rpc", json=req)
    assert r.status_code == 401
    assert r.json()["detail"].startswith("Sign in with an API key")
    r = await client.post("/api/session/rpc", json=req, headers=them)
    assert r.json()["result"] == {"collections": []}


# ── session: collections ─────────────────────────────────────────────────────


async def test_collections_are_made_read_renamed_pinned_and_deleted(
    client: AsyncClient,
) -> None:
    made = await ok(client, "session", "collection_create", {"req": {"title": " Coral  reefs "}})
    assert set(made) == {"cid", "title", "title_auto", "created_ms", "updated_ms", "pinned"}
    assert made["title"] == "Coral reefs" and not made["title_auto"]
    assert made["created_ms"] > 1_700_000_000_000
    cid = made["cid"]
    draft = await ok(client, "sources", "draft_create")
    assert uuid.UUID(draft) and draft != cid

    listed = (await ok(client, "session", "collection_list"))["collections"]
    assert {c["cid"] for c in listed} == {cid, draft}
    one = next(c for c in listed if c["cid"] == cid)
    assert set(one) == {
        "cid", "title", "title_auto", "created_ms", "updated_ms", "pinned", "sources",
        "decks", "audios", "maps", "notes", "preparing", "failed", "cover_version",
    }  # fmt: skip

    assert await ok(client, "session", "collection_retitle", {"req": {"cid": cid, "title": "R"}})
    assert await ok(client, "session", "collection_pin", {"req": {"cid": cid, "pinned": True}})
    got = await ok(client, "session", "collection_get", {"cid": cid})
    assert got["found"] and got["outputs"] == []
    assert (got["collection"]["title"], got["collection"]["pinned"]) == ("R", True)

    # An empty collection keeps the cover drawn from its title; nothing to ask.
    assert await ok(client, "session", "collection_cover_refresh", {"cid": cid}) is True

    gone = str(uuid.uuid4())
    for m, p in [
        ("collection_retitle", {"req": {"cid": gone, "title": "x"}}),
        ("collection_pin", {"req": {"cid": "not-a-uuid", "pinned": True}}),
        ("collection_cover_refresh", {"cid": gone}),
        ("collection_delete", {"cid": gone}),
    ]:
        assert await ok(client, "session", m, p) is False, m
    assert await ok(client, "session", "collection_get", {"cid": "s1700000000000"}) == {
        "found": False,
        "outputs": [],
    }

    assert await ok(client, "session", "collection_delete", {"cid": cid}) is True
    assert (await ok(client, "session", "collection_get", {"cid": cid}))["found"] is False


async def test_a_refusal_is_the_rest_sentence(client: AsyncClient) -> None:
    cid = await _cid(client)
    await _rows("UPDATE collections SET read_only = true WHERE id = :c RETURNING id", c=cid)
    e = await err(client, "session", "collection_retitle", {"req": {"cid": cid, "title": "x"}})
    assert e["code"] == -32602 and e["data"] == {"status": 403}
    assert "read-only copy" in e["message"]
    e = await err(client, "session", "collection_create", {"req": {"title": "x" * 300}})
    assert e["code"] == -32602 and e["message"].startswith("Title:")


# ── session: outputs and playback ────────────────────────────────────────────


async def test_an_output_reads_in_the_old_shape(client: AsyncClient) -> None:
    cid = await _cid(client)
    sid = await _output(client, cid)

    (summary,) = (await ok(client, "session", "session_list"))["sessions"]
    assert summary == {
        "sid": sid,
        "title": "Deck",
        "state": "ready",
        "slide_count": 1,
        "speakers": 1,
        "kind": "session",
        "audio_format": "",
        "duration_ms": 1500,
        "description": "Reefs are built by polyps.",
        "created_ms": summary["created_ms"],
        "pinned": False,
        "collection": cid,
        "spent_usd": 0.02,
        "spent_known": True,
    }
    got = await ok(client, "session", "collection_get", {"cid": cid})
    assert got["outputs"] == [summary] and got["collection"]["decks"] == 1

    got = await ok(client, "session", "session_get", {"sid": sid})
    assert got["found"]
    s = got["session"]
    assert (s["sid"], s["collection"], s["collection_name"], s["style"]) == (
        sid,
        cid,
        sid,
        "editorial",
    )
    assert "prep_job_sid" not in s and "failure" not in s and "audio" not in s
    assert s["speakers"] == [
        {"speaker_id": "host", "voice_id": "af_bella", "display_name": "Bella", "role": ""}
    ]
    (slide,) = s["slides"]
    assert slide["slide_url"] == f"/api/sessions/{sid}/slides/0" and "on_slide" not in slide
    assert slide["slide_ref"] == {"collection": "c", "presentation": "p", "slide": "polyps"}
    voiced, silent = slide["lines"]
    assert voiced["audio_url"] == f"/api/sessions/{sid}/audio/l1"
    assert voiced["duration_ms"] == 1500
    assert "audio_url" not in silent and "duration_ms" not in silent and silent["cues"] == []

    for gone in (str(uuid.uuid4()), "s1700000000000"):
        assert await ok(client, "session", "session_get", {"sid": gone}) == {"found": False}


async def test_an_output_is_renamed_pinned_played_and_deleted(client: AsyncClient) -> None:
    sid = await _output(client, await _cid(client))
    assert await ok(client, "session", "session_retitle", {"req": {"sid": sid, "title": " R "}})
    assert await ok(client, "session", "session_pin", {"req": {"sid": sid, "pinned": True}})
    s = (await ok(client, "session", "session_list"))["sessions"][0]
    assert (s["title"], s["pinned"]) == ("R", True)
    e = await err(client, "session", "session_retitle", {"req": {"sid": sid, "title": "  "}})
    assert e["code"] == -32602 and e["message"].startswith("A title cannot be empty")

    idle = {"slide_ordinal": 0, "line_id": "", "offset_ms": 0, "state": "idle"}
    assert await ok(client, "session", "playback_get", {"sid": sid}) == idle
    at = {"sid": sid, "slide_ordinal": 0, "line_id": "l1", "offset_ms": 700}
    for m, state in [
        ("playback_play", "playing"),
        ("playback_progress", "playing"),
        ("playback_pause", "paused"),
    ]:
        head = await ok(client, "session", m, {"req": at})
        assert head == {"slide_ordinal": 0, "line_id": "l1", "offset_ms": 700, "state": state}
    done = await ok(client, "session", "playback_finish", {"sid": sid})
    assert done == {"slide_ordinal": 0, "line_id": "l1", "offset_ms": 700, "state": "finished"}
    # Persisted: REST reads the same playhead.
    assert (await client.get(f"/api/sessions/{sid}/playback")).json()["state"] == "finished"
    e = await err(client, "session", "playback_get", {"sid": str(uuid.uuid4())})
    assert e["data"] == {"status": 404}

    assert await ok(client, "session", "session_delete", {"sid": sid}) is True
    assert await ok(client, "session", "session_delete", {"sid": sid}) is False
    assert (
        await ok(client, "session", "session_pin", {"req": {"sid": sid, "pinned": True}}) is False
    )
    assert (
        await ok(client, "session", "session_retitle", {"req": {"sid": sid, "title": "x"}}) is False
    )


async def test_a_build_is_estimated_started_and_followed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_build(monkeypatch, tmp_path)
    cid = await _cid(client)
    for i in range(2):
        await add_note(client, cid, f"Coral reefs, part {i}. Reefs are built by polyps. " * 8)

    req = {"sid": "", "collection": cid, "slide_count": 5}
    est = await ok(client, "session", "session_estimate", {"req": req})
    assert (est["sources"], est["slides"]) == (2, 5) and est["total_high_usd"] > 0
    rest = (
        await client.post(
            f"/api/collections/{cid}/outputs/estimate", json={"kind": "slides", "slide_count": 5}
        )
    ).json()
    # The old shape, without what the REST API added since.
    assert set(est) == set(rest) - {"model", "facts"} and est["lines"] == rest["lines"]

    built = await ok(client, "session", "session_build", {"req": req})
    assert built["accepted"] and built["sid"] != cid
    (job,) = await _rows("SELECT id, kind, session_id FROM jobs")
    assert built["prep_job_sid"] == str(job["id"]) and str(job["session_id"]) == built["sid"]
    got = (await ok(client, "session", "session_get", {"sid": built["sid"]}))["session"]
    assert got["state"] == "preparing" and got["prep_job_sid"] == built["prep_job_sid"]

    # Without a collection the sid is the collection, as before collections.
    audio = await ok(
        client,
        "session",
        "session_build",
        {"req": {"sid": cid, "audio_format": "brief", "focus": "polyps"}},
    )
    s = (await ok(client, "session", "session_get", {"sid": audio["sid"]}))["session"]
    assert s["audio"]["format"] == "brief" and s["audio"]["focus"] == "polyps"
    kinds = {x["sid"]: x["kind"] for x in (await ok(client, "session", "session_list"))["sessions"]}
    assert kinds == {built["sid"]: "session", audio["sid"]: "audio"}

    prepared = await ok(
        client,
        "session",
        "session_prepare",
        {
            "req": {
                "sid": "",
                "title": "Mine",
                "resource_dir": "",
                "speakers": [{}],
                "collection": cid,
            }
        },
    )
    s = (await ok(client, "session", "session_get", {"sid": prepared["sid"]}))["session"]
    assert s["title"] == "Mine" and len(s["speakers"]) == 1

    e = await err(
        client,
        "session",
        "session_prepare",
        {"req": {"sid": "x", "title": "", "resource_dir": "/srv/notes", "speakers": []}},
    )
    assert e["code"] == -32602 and "session_build" in e["message"]
    e = await err(client, "session", "session_build", {"req": {"sid": ""}})
    assert e["message"].startswith("Say which collection to build from")
    e = await err(client, "session", "session_build", {"req": {"sid": cid, "audio_format": "x"}})
    assert e["code"] == -32602
    empty = await _cid(client, "Empty")
    e = await err(client, "session", "session_estimate", {"req": {"sid": empty}})
    assert e["message"] == "Add a source first: a link, a note, or a topic to research."


async def test_a_question_about_an_output_is_answered_from_its_sources(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _cid(client)
    await add_note(client, cid, REEFS, title="Reefs")
    sid = await _output(client, cid)
    model.answers(says("  Polyps build them, slowly.  "))
    got = await ok(
        client,
        "session",
        "session_ask",
        {"req": {"sid": sid, "question": "Who builds reefs?", "slide_ordinal": 0}},
    )
    assert got == {"answer": "Polyps build them, slowly."}
    user = model.said_to(0, "user")
    assert 'on the slide "Polyps"' in user and "Reefs are built by polyps. Slowly." in user
    assert "Coral reefs cover less than one percent" in user
    assert user.endswith("Question: Who builds reefs?")
    assert '"Deck"' in model.said_to(0, "system")
    (row,) = await _rows("SELECT kind, session_id FROM usage_events")
    assert row["kind"] == "ask" and str(row["session_id"]) == sid

    model.answers(fails(402, "insufficient credits"))
    e = await err(client, "session", "session_ask", {"req": {"sid": sid, "question": "Why?"}})
    assert e["code"] == -32603 and e["data"] == {"status": 402}
    assert e["message"].startswith("The AI account is out of credit")

    waiting = await _output(client, cid, state="preparing")
    e = await err(client, "session", "session_ask", {"req": {"sid": waiting, "question": "Q?"}})
    assert e["data"] == {"status": 409}
    e = await err(client, "session", "session_ask", {"req": {"sid": sid, "question": " "}})
    assert e["message"] == "The question is empty. Write a question, then ask again."


# ── sources ──────────────────────────────────────────────────────────────────


async def test_notes_files_and_pages_are_added_listed_and_removed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    cid = await _cid(client)
    note = await ok(
        client, "sources", "source_add_text", {"req": {"sid": cid, "text": REEFS, "title": "Reefs"}}
    )
    assert note == {"url": "", "ok": True, "title": "Reefs", "chars": note["chars"], "error": ""}
    assert note["chars"] > len(REEFS)
    empty = await ok(client, "sources", "source_add_text", {"req": {"sid": cid, "text": "  "}})
    assert not empty["ok"] and empty["error"].startswith("The note is empty.")

    data = base64.b64encode(b"# Polyps\n\nPolyps build reefs_slowly * 3.").decode()
    f = await ok(
        client,
        "sources",
        "source_add_file",
        {"req": {"sid": cid, "name": "polyps.md", "data_base64": data}},
    )
    assert f["ok"] and f["title"] == "polyps" and f["error"] == ""
    bad = await ok(
        client,
        "sources",
        "source_add_file",
        {"req": {"sid": cid, "name": "x.exe", "data_base64": base64.b64encode(b"MZ").decode()}},
    )
    assert not bad["ok"] and bad["error"].endswith(".")
    e = await err(
        client,
        "sources",
        "source_add_file",
        {"req": {"sid": cid, "name": "a.md", "data_base64": "%%%"}},
    )
    assert e["code"] == -32602 and "base64" in e["message"]

    def pages(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=PAGE)

    fake_web(monkeypatch, pages)
    got = await ok(
        client,
        "sources",
        "source_add_urls",
        {"req": {"sid": cid, "urls": ["https://example.com/reefs", "http://127.0.0.1/admin"]}},
    )
    assert got["added"] == 1
    page, inside = got["results"]
    assert page["ok"] and page["title"] == "Coral reefs" and page["error"] == ""
    assert page["url"] == "https://example.com/reefs"
    assert not inside["ok"] and inside["error"].startswith("That link points inside")
    e = await err(
        client, "sources", "source_add_urls", {"req": {"sid": cid, "urls": ["not a link"]}}
    )
    assert e["code"] == -32602

    listed = (await ok(client, "sources", "source_list", {"sid": cid}))["sources"]
    assert [x["title"] for x in listed] == ["Reefs", "polyps", "Coral reefs"]
    assert set(listed[0]) == {"name", "title", "url", "chars"}
    name = listed[0]["name"]
    assert await ok(client, "sources", "source_remove", {"req": {"sid": cid, "name": name}})
    assert not await ok(client, "sources", "source_remove", {"req": {"sid": cid, "name": name}})
    e = await err(client, "sources", "source_list", {"sid": str(uuid.uuid4())})
    assert e["data"] == {"status": 404}
    e = await err(client, "sources", "source_add_text", {"req": {"sid": "nope", "text": "x"}})
    assert e["data"] == {"status": 404}


async def test_the_web_is_searched_and_a_question_answered_with_citations(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)

    async def search(model: str, query: str) -> list[research.Hit]:
        return [research.Hit(title=f"About {query}", url="https://example.com/r", snippet="s")]

    monkeypatch.setattr(research, "web_search", search)
    got = await ok(client, "sources", "web_search", {"query": " reefs "})
    assert got == {
        "hits": [{"title": "About reefs", "url": "https://example.com/r", "snippet": "s"}]
    }
    e = await err(client, "sources", "web_search", {"query": " "})
    assert e["code"] == -32602

    cid = await _cid(client)
    name = await add_note(client, cid, REEFS, title="Reefs")
    model.answers(says("Reefs shelter a quarter of marine species [1]."))
    got = await ok(
        client, "sources", "source_ask", {"req": {"sid": cid, "question": "Why do reefs matter?"}}
    )
    assert got["answer"] == "Reefs shelter a quarter of marine species [1]."
    (c,) = got["citations"]
    assert (c["n"], c["name"], c["title"], c["url"]) == (1, name, "Reefs", "")
    e = await err(client, "sources", "source_ask", {"req": {"sid": cid, "question": ""}})
    assert e["message"] == "The question is empty. Write a question, then ask again."


async def test_deep_research_waits_for_its_job_and_reports_the_source(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rpc_methods, "RESEARCH_POLL_S", 0.02)
    cid = await _cid(client)

    async def worker(succeed: bool) -> None:
        """What the research job does, as far as the caller can see."""
        jobs: list[dict[str, Any]] = []
        for _ in range(500):
            if jobs := await _rows("SELECT id FROM jobs WHERE status = 'queued'"):
                break
            await asyncio.sleep(0.02)
        if succeed:
            await add_note(client, cid, REEFS, title="Research report: reefs (3 sources)")
            await _rows("UPDATE sources SET kind = 'research' RETURNING id")
            await _rows(
                "UPDATE jobs SET status = 'done' WHERE id = :j RETURNING id", j=jobs[0]["id"]
            )
        else:
            await _rows(
                "UPDATE jobs SET status = 'failed', error = 'No page on the topic could be read.'"
                " WHERE id = :j RETURNING id",
                j=jobs[0]["id"],
            )

    req = {"req": {"sid": cid, "topic": "reefs"}}
    got, _ = await asyncio.gather(ok(client, "sources", "deep_research", req), worker(True))
    assert got == {
        "url": "",
        "ok": True,
        "title": "Research report: reefs (3 sources)",
        "chars": got["chars"],
        "error": "",
    }
    got, _ = await asyncio.gather(ok(client, "sources", "deep_research", req), worker(False))
    assert got["ok"] is False and got["error"] == "No page on the topic could be read."

    monkeypatch.setattr(rpc_methods, "RESEARCH_WAIT_S", 0.1)
    got = await ok(client, "sources", "deep_research", req)
    assert not got["ok"] and got["error"].startswith("The research is still running")
    e = await err(client, "sources", "deep_research", {"req": {"sid": cid, "topic": " "}})
    assert e["code"] == -32602


# ── mind maps and notes ──────────────────────────────────────────────────────


async def test_a_mind_map_is_made_listed_read_renamed_and_deleted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _cid(client)
    name = await add_note(client, cid, REEFS, title="Reefs")
    est = await ok(client, "mindmap", "mindmap_estimate", {"sid": cid})
    assert set(est) == {
        "sources", "chars", "model", "input_tokens", "output_tokens", "cost_usd",
        "cost_high_usd", "priced",
    }  # fmt: skip
    assert est["sources"] == 1 and est["priced"]

    model.answers(says(OUTLINE))
    m = await ok(client, "mindmap", "mindmap_create", {"req": {"sid": cid, "focus": "threats"}})
    assert (m["sid"], m["title"], m["focus"], m["sources"]) == (
        cid,
        "Coral reefs",
        "threats",
        [name],
    )
    assert m["root"]["name"] == "Coral reefs" and m["node_count"] == 13 and m["dropped"] == 2
    assert m["created_ms"] > 0 and "collection_id" not in m and "shape" not in m

    summary = {k: m[k] for k in ("id", "sid", "title", "focus", "node_count", "created_ms")}
    summary |= {"sources": [name], "shape": [1, 3, 2, 2]}
    assert (await ok(client, "mindmap", "mindmap_list", {"sid": cid}))["maps"] == [summary]
    assert (await ok(client, "mindmap", "mindmap_list_all"))["maps"] == [summary]
    ref = {"req": {"sid": cid, "id": m["id"]}}
    assert await ok(client, "mindmap", "mindmap_get", ref) == m

    renamed = {"req": {"sid": cid, "id": m["id"], "title": "Reef map"}}
    assert await ok(client, "mindmap", "mindmap_retitle", renamed) is True
    assert (await ok(client, "mindmap", "mindmap_get", ref))["title"] == "Reef map"
    e = await err(client, "mindmap", "mindmap_retitle", {"req": {**ref["req"], "title": ""}})
    assert e["message"].startswith("A title cannot be empty")
    assert await ok(client, "mindmap", "mindmap_delete", ref) is True
    assert await ok(client, "mindmap", "mindmap_delete", ref) is False
    assert await ok(client, "mindmap", "mindmap_retitle", renamed) is False
    e = await err(client, "mindmap", "mindmap_get", ref)
    assert e["data"] == {"status": 404} and e["message"].startswith("That mind map")

    model.answers(fails(402, "insufficient credits"))
    e = await err(client, "mindmap", "mindmap_create", {"req": {"sid": cid}})
    assert e["code"] == -32603 and e["message"].startswith("The AI account is out of credit")


async def test_study_notes_are_written_listed_read_renamed_and_deleted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _cid(client)
    moshi = await add_note(client, cid, MOSHI, title="Moshi paper")
    await add_note(client, cid, MIMI, title="Mimi codec")
    est = await ok(client, "notes", "notes_estimate", {"sid": cid})
    assert est["sources"] == 2 and est["cost_high_usd"] == pytest.approx(2 * est["cost_usd"])

    model.answers(says(NOTES))
    n = await ok(client, "notes", "notes_create", {"req": {"sid": cid, "sources": None}})
    assert (n["sid"], n["title"], n["dropped"]) == (cid, "Moshi notes", 2)
    assert [i["heading"] for i in n["ideas"]] == ["Latency", "Codec"]
    assert n["quiz"] == [{"question": "What is Mimi?", "answer": "A neural audio codec [2]."}]
    assert n["citations"][0]["name"] == moshi and n["markdown"].startswith("#")
    assert "idea_list" not in n and "collection_id" not in n

    (summary,) = (await ok(client, "notes", "notes_list", {"sid": cid}))["notes"]
    assert summary == {
        "id": n["id"],
        "sid": cid,
        "title": "Moshi notes",
        "focus": "",
        "created_ms": n["created_ms"],
        "sources": n["sources"],
        "ideas": 2,
        "questions": 1,
        "terms": 1,
        "headings": ["Latency", "Codec"],
    }
    assert (await ok(client, "notes", "notes_list_all"))["notes"] == [summary]
    ref = {"req": {"sid": cid, "id": n["id"]}}
    assert await ok(client, "notes", "notes_get", ref) == n
    renamed = {"req": {"sid": cid, "id": n["id"], "title": "Moshi"}}
    assert await ok(client, "notes", "notes_retitle", renamed) is True
    assert (await ok(client, "notes", "notes_get", ref))["title"] == "Moshi"
    assert await ok(client, "notes", "notes_delete", ref) is True
    assert await ok(client, "notes", "notes_delete", ref) is False
    assert await ok(client, "notes", "notes_retitle", renamed) is False


# ── settings ─────────────────────────────────────────────────────────────────


async def test_settings_and_styles_read_in_the_old_shape(client: AsyncClient) -> None:
    got = await ok(client, "settings", "settings_get")
    assert got["tabs"] and set(got["tabs"][0]) == {"id", "label", "note", "advanced"}
    rest = (await client.get("/api/settings")).json()
    assert len(got["settings"]) == len(rest["settings"]) == 29
    for old in got["settings"]:
        assert "suggestions" not in old and "scope" not in old
        assert ("min" in old) == (old["kind"] == "number"), old["key"]

    saved = await ok(
        client, "settings", "settings_set", {"req": {"key": st.LANGUAGE_KEY, "value": "Hindi"}}
    )
    assert saved["note"] == "" and saved["setting"]["value"] == "Hindi"
    assert (await client.get("/api/settings")).json()["settings"] != rest["settings"]
    e = await err(client, "settings", "settings_set", {"req": {"key": "NOPE", "value": "x"}})
    assert e["code"] == -32602 and e["data"] == {"status": 404}
    e = await err(
        client, "settings", "settings_set", {"req": {"key": st.SLIDE_COUNT_KEY, "value": "99"}}
    )
    assert e["code"] == -32602 and e["data"] == {"status": 422}

    styles = (await ok(client, "settings", "styles_list"))["styles"]
    rest = (await client.get("/api/styles")).json()
    assert styles == [{k: v for k, v in x.items() if k != "thumbnail"} for x in rest]


# ── one person's things are not another's ────────────────────────────────────


async def test_nobody_reaches_another_persons_things(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _cid(client)
    await add_note(client, cid, REEFS, title="Reefs")
    sid = await _output(client, cid)
    them = await other_person(client, "them@example.com")

    async def as_them(domain: str, method: str, params: Any) -> dict[str, Any]:
        return await call(client, domain, method, params, them)

    for domain, method, params, answer in [
        ("session", "collection_get", {"cid": cid}, {"found": False, "outputs": []}),
        ("session", "collection_retitle", {"req": {"cid": cid, "title": "Mine"}}, False),
        ("session", "collection_pin", {"req": {"cid": cid, "pinned": True}}, False),
        ("session", "collection_delete", {"cid": cid}, False),
        ("session", "session_get", {"sid": sid}, {"found": False}),
        ("session", "session_delete", {"sid": sid}, False),
        ("session", "session_pin", {"req": {"sid": sid, "pinned": True}}, False),
        ("sources", "source_remove", {"req": {"sid": cid, "name": "x"}}, False),
    ]:
        assert (await as_them(domain, method, params))["result"] == answer, method
    for domain, method, params in [
        ("sources", "source_list", {"sid": cid}),
        ("sources", "source_add_text", {"req": {"sid": cid, "text": "Mine now."}}),
        ("sources", "source_ask", {"req": {"sid": cid, "question": "What?"}}),
        ("session", "session_build", {"req": {"sid": "", "collection": cid}}),
        ("session", "session_ask", {"req": {"sid": sid, "question": "What?"}}),
        ("session", "playback_get", {"sid": sid}),
        ("mindmap", "mindmap_list", {"sid": cid}),
        ("mindmap", "mindmap_create", {"req": {"sid": cid}}),
        ("notes", "notes_estimate", {"sid": cid}),
    ]:
        e = (await as_them(domain, method, params))["error"]
        assert e["data"] == {"status": 404}, method
    assert (await as_them("session", "session_list", None))["result"] == {"sessions": []}
    assert (await as_them("session", "collection_list", None))["result"] == {"collections": []}
    # And all of it is still the owner's.
    got = await ok(client, "session", "collection_get", {"cid": cid})
    assert got["collection"]["title"] == "Reefs" and got["collection"]["sources"] == 1
    assert len(got["outputs"]) == 1
