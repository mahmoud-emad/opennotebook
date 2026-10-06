# ruff: noqa: E501
"""The Ask chat: the agent loop (ported from `agent.rs`), the `/` commands
(ported from the UI's `chat.rs`, now run by the server), and the routes that
stream them, against a mocked model and mocked web pages."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.agent import commands, loop
from opennotebook.ai import client as client_module
from opennotebook.ai.client import Ai
from opennotebook.db.session import engine
from tests.conftest import other_person
from tests.model import Model, Reply, add_note, fails, install, says, spent
from tests.web import fake_web

# ── ported from agent.rs ─────────────────────────────────────────────────────


def test_a_failed_page_is_replaced_by_the_next_untried_result_on_a_live_host() -> None:
    pool = ["https://dead.org/a", "https://dead.org/b", "https://ok.org/c", "https://ok.org/d"]
    tried = {"https://dead.org/a"}
    dead = {"dead.org"}
    assert loop.next_replacement(pool, tried, dead) == "https://ok.org/c"
    tried |= {"https://ok.org/c", "https://ok.org/d"}
    assert loop.next_replacement(pool, tried, dead) is None


def test_a_request_to_build_is_recognised_and_a_hold_is_respected() -> None:
    assert loop.asked_to_build(
        "Help me to understnad how linux kernel is implmeneted and build the session once you collect the resources"
    )
    assert loop.asked_to_build("build")
    assert not loop.asked_to_build("add more resources")
    assert not loop.asked_to_build("find sources but don't build yet")


def test_the_models_reasoning_is_never_shown() -> None:
    reply = (
        "<thinking> I'll now list only the pages that were added.</thinking>\n\n"
        "Here are the sources that I've added:\n\nTour of the Linux kernel source"
    )
    assert loop.without_reasoning(reply) == (
        "Here are the sources that I've added:\n\nTour of the Linux kernel source"
    )
    assert loop.without_reasoning("<THINK>hmm</THINK>Done.") == "Done."
    assert loop.without_reasoning("<thinking>never closed\n\nSay build.") == "Say build."
    assert loop.without_reasoning("No tags here.") == "No tags here."


def test_a_failed_page_never_survives_into_the_reply() -> None:
    failed = ["https://dead.example/x.pdf"]
    text_ = "Added these:\n1. [Good](https://ok.example/a)\n2. [Dead](https://dead.example/x.pdf)\nSay build."
    out = loop.without_failed(text_, failed)
    assert "dead.example" not in out
    assert "ok.example" in out and "Say build." in out
    assert loop.without_failed("plain", []) == "plain"


def _names(web: bool) -> list[str]:
    return [t["function"]["name"] for t in loop.tools(web)]


def test_web_tools_exist_only_with_research_on() -> None:
    assert "web_search" in _names(True)
    assert "web_search" not in _names(False)
    assert "start_build" in _names(False)


def test_the_agent_knows_everything_it_can_make() -> None:
    """Every page can make every kind; what the page picked is the default."""
    names = _names(False)
    assert "start_build" in names and "ask_sources" in names
    p = loop.system_prompt([], True, "mindmap", "")
    for k in ["slides", "audio overview", "mind map", "study notes", "deep_research", "/help"]:
        assert k in p, f"prompt lacks {k}"
    assert "picked mindmap" in p
    p = loop.system_prompt([], False, "", "")
    assert "picked slides" in p, "a page that sends nothing builds slides"
    assert "deep_research" not in p, "no research tools with research off"
    assert "read-only copy" in loop.system_prompt([], True, "", "", read_only=True)


def test_a_question_goes_to_the_sources_before_the_web() -> None:
    """A question the sources may answer is asked of them first; the web is
    searched only when they do not cover it, never answered from alone."""
    p = loop.system_prompt(["vaccines.md"], True, "", "")
    assert "a question goes to ask_sources first" in p
    assert "only when ask_sources says the sources do not cover it" in p
    assert "never answer from search results alone" in p
    assert "the sources do not cover it, find the material yourself" in p


def test_a_kind_maps_to_what_the_page_knows_it_by() -> None:
    assert loop.wire_kind("mindmap", "session") == "mindmap"
    assert loop.wire_kind("slides", "audio") == "session"
    assert loop.wire_kind("", "audio") == "audio"
    assert loop.wire_kind("", "") == "session"
    assert loop.wire_kind("nonsense", "notes") == "session"


# ── ported from create.rs ────────────────────────────────────────────────────


def test_a_pasted_link_is_material_and_a_short_reply_is_not() -> None:
    urls, rest = loop.split_material("https://arxiv.org/abs/2410.00037")
    assert urls == ["https://arxiv.org/abs/2410.00037"]
    assert rest == ""
    urls, rest = loop.split_material("read (https://x.dev/a). what is it about?")
    assert urls == ["https://x.dev/a"], "punctuation kept"
    assert rest == "read what is it about?"
    urls, rest = loop.split_material("go on")
    assert urls == [] and len(rest) < loop.NOTE_CHARS


# ── ported from the UI's chat.rs ─────────────────────────────────────────────


def test_a_slash_names_a_command_and_the_rest_is_its_argument() -> None:
    assert commands.parse_command("/mindmap") == ("mindmap", "")
    assert commands.parse_command("  /Search  rust async  ") == ("search", "rust async")
    assert commands.parse_command("/nope x") == ("nope", "x")
    assert commands.find("nope") is None
    assert commands.parse_command("no slash") is None


def test_help_lists_every_command() -> None:
    h = commands.help_text()
    for c in commands.COMMANDS:
        assert f"/{c.name}" in h


# ── the routes ───────────────────────────────────────────────────────────────

REEFS = (
    "Coral reefs are built by colonies of tiny animals called polyps, which secrete calcium "
    "carbonate skeletons. Over thousands of years these skeletons accumulate into vast "
    "structures. Reefs cover less than one percent of the ocean floor, yet they shelter "
    "roughly a quarter of all marine species."
)
KELP = (
    "Kelp forests grow along cold, nutrient-rich coasts. The giant kelp can grow half a metre "
    "a day, and its forests shelter fish, sea otters and sea urchins, which in turn shape "
    "how the forest grows. Without otters, urchins can strip a forest bare in a few seasons."
)


def _page(title: str, body: str) -> str:
    return (
        f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<p>{body}</p><p>{body}</p></article></body></html>"
    )


def _web(request: httpx.Request) -> httpx.Response:
    match (request.headers.get("host", ""), request.url.path):
        case ("example.com", "/reefs"):
            return httpx.Response(200, html=_page("Coral reefs", REEFS))
        case ("kelp.example", "/kelp"):
            return httpx.Response(200, html=_page("Kelp forests", KELP))
        case _:
            return httpx.Response(404, text="nope")


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web, as a few pages; every other address is a 404."""
    fake_web(monkeypatch, _web)


@pytest.fixture(autouse=True)
async def empty_queue() -> AsyncIterator[None]:
    yield
    async with engine().begin() as c:
        await c.execute(text("TRUNCATE procrastinate_jobs, procrastinate_workers CASCADE"))


def _usage() -> dict[str, Any]:
    return {"prompt_tokens": 100, "completion_tokens": 20}


def calls(*named: tuple[str, dict[str, Any]]) -> Reply:
    """The agent's model, calling tools."""
    body = {
        "model": "google/gemini-2.5-flash-lite",
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call{i}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (name, args) in enumerate(named)
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": _usage(),
    }
    return lambda _: httpx2.Response(200, json=body)


def found(*urls: str) -> Reply:
    """The search model, citing what it found."""
    body = {
        "model": "perplexity/sonar",
        "citations": list(urls),
        "choices": [{"message": {"content": "Reefs and kelp."}, "finish_reason": "stop"}],
        "usage": _usage(),
    }
    return lambda _: httpx2.Response(200, json=body)


def events(r: httpx.Response) -> list[dict[str, Any]]:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return [json.loads(ln[5:]) for ln in r.text.split("\n") if ln.startswith("data:")]


def kinds(evs: list[dict[str, Any]]) -> list[str]:
    return [e["t"] for e in evs]


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def _history(client: AsyncClient, cid: str) -> list[dict[str, Any]]:
    r = await client.get(f"/api/collections/{cid}/chat")
    assert r.status_code == 200, r.text
    return r.json()


async def _rows(sql: str, **kw: Any) -> list[dict[str, Any]]:
    async with engine().begin() as c:
        return [dict(r) for r in (await c.execute(text(sql), kw)).mappings()]


async def _say(client: AsyncClient, cid: str, said: str, **kw: Any) -> list[dict[str, Any]]:
    return events(await client.post(f"/api/collections/{cid}/chat", json={"text": said, **kw}))


async def _command(
    client: AsyncClient, cid: str, name: str, arg: str = "", **kw: Any
) -> list[dict[str, Any]]:
    return events(
        await client.post(
            f"/api/collections/{cid}/chat/commands", json={"name": name, "arg": arg, **kw}
        )
    )


async def test_a_pasted_link_is_read_before_the_model_answers_and_the_turn_is_kept(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, web: None
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    model.answers(says("<think>it is in</think>The reef page is in. Say build when ready."))
    evs = await _say(client, cid, "what about this https://example.com/reefs")
    assert kinds(evs) == ["step", "step_done", "source", "thinking", "reply", "state"]
    assert evs[0] == {
        "t": "step",
        "id": "a1",
        "kind": "fetch",
        "text": "Reading",
        "detail": "example.com",
    }
    assert evs[1]["ok"] and evs[1]["text"].startswith("added “Coral reefs”")
    assert evs[2]["src"]["title"] == "Coral reefs" and evs[2]["src"]["ok"]
    assert evs[4]["text"] == "The reef page is in. Say build when ready.", "no reasoning shown"
    assert evs[5]["ready"] is True

    # The model knew the page was in before it answered.
    body = model.bodies[0]
    assert body["model"] == "google/gemini-2.5-flash-lite"
    assert body["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in body["tools"]} >= {"web_search", "start_build"}
    assert "Sources already added: coral_reefs.md" in model.said_to(0, "system")
    assert model.said_to(0, "user") == "what about this https://example.com/reefs"

    said, answered = await _history(client, cid)
    assert said["role"] == "user" and said["text"] == "what about this https://example.com/reefs"
    assert answered["role"] == "assistant"
    assert answered["text"] == "The reef page is in. Say build when ready."
    (st,) = answered["steps"]
    assert st["kind"] == "fetch" and st["status"] == "ok" and st["note"].startswith("added")
    assert [r["kind"] for r in await spent()] == ["agent"]

    # The next turn carries the conversation so far.
    model.answers(says("Yes."))
    await _say(client, cid, "and again?")
    roles = [m["role"] for m in model.bodies[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


async def test_the_agent_searches_adds_replaces_a_dead_page_and_never_names_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, web: None
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    dead = "https://dead.example/gone"
    model.answers(
        calls(("web_search", {"query": "reefs and kelp"})),
        found("https://example.com/reefs", dead, "https://kelp.example/kelp"),
        calls(("add_sources", {"urls": ["https://example.com/reefs", dead]})),
        says(f"Added [Reefs](https://example.com/reefs) and [Gone]({dead})."),
        says(f"Added the reef and kelp pages.\nNot {dead}"),
    )
    evs = await _say(client, cid, "help me understand reefs and kelp")
    steps = [(e["text"], e["detail"]) for e in evs if e["t"] == "step"]
    assert steps == [
        ("Searching the web", "reefs and kelp"),
        ("Reading", "example.com"),
        ("Reading", "dead.example"),
        ("Trying another page instead", "kelp.example"),
    ]
    results = [(e["ok"], e["text"]) for e in evs if e["t"] == "step_done"]
    assert results[0] == (True, "3 results")
    assert results[2] == (False, "The page does not exist (404). Check the link.")
    assert [e["src"]["title"] for e in evs if e["t"] == "source"] == [
        "Coral reefs",
        "Kelp forests",
    ]
    replies = [e["text"] for e in evs if e["t"] == "reply"]
    # Told once which page failed, then the line naming it dropped anyway.
    assert replies == ["Added the reef and kelp pages."]
    assert "(studio) Your reply names pages that could NOT be read" in json.dumps(
        model.bodies[4]["messages"]
    )
    # What add_sources told the model, failure and replacement included.
    tool_said = [m for m in model.bodies[3]["messages"] if m["role"] == "tool"]
    assert "could not read https://dead.example/gone" in tool_said[-1]["content"]
    assert "replaced a page that failed with https://kelp.example/kelp" in tool_said[-1]["content"]
    assert sorted(r["kind"] for r in await spent()) == ["agent"] * 4 + ["web_search"]
    listed = (await client.get(f"/api/collections/{cid}/sources")).json()
    assert sorted(s["title"] for s in listed) == ["Coral reefs", "Kelp forests"]


async def test_a_search_that_adds_nothing_is_sent_back_once_to_add(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, web: None
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    model.answers(
        calls(("web_search", {"query": "reefs"})),
        found("https://example.com/reefs"),
        says("I found a reef page."),
        calls(("add_sources", {"urls": ["https://example.com/reefs"]})),
        says("The reef page is in."),
    )
    evs = await _say(client, cid, "reefs please")
    assert [e["text"] for e in evs if e["t"] == "reply"] == ["The reef page is in."]
    assert "(studio) Nothing has been added yet" in json.dumps(model.bodies[3]["messages"])


async def test_an_answer_from_the_sources_is_relayed_whole_with_its_citations(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    name = await add_note(client, cid, REEFS)
    model.answers(
        calls(("ask_sources", {"question": "Who builds reefs?"})),
        says("Polyps build them [1]."),
    )
    evs = await _say(client, cid, "who builds reefs?")
    assert kinds(evs) == ["thinking", "step", "step_done", "reply", "state"]
    assert evs[1]["kind"] == "read" and evs[1]["text"] == "Reading your sources"
    assert evs[2]["text"] == "1 passage cited"
    assert evs[3]["text"] == "Polyps build them [1]."
    assert [(c["n"], c["name"]) for c in evs[3]["citations"]] == [(1, name)]
    # Ended the turn: the model was not asked to paraphrase it.
    assert len(model.bodies) == 2
    kept = (await _history(client, cid))[-1]
    assert kept["text"] == "Polyps build them [1]." and kept["citations"][0]["name"] == name
    assert sorted(r["kind"] for r in await spent()) == ["agent", "ask"]


async def test_start_build_starts_the_job_on_the_server(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(calls(("start_build", {"kind": "audio", "title": "Reefs"})))
    evs = await _say(
        client,
        cid,
        "make an audio overview",
        audio_format="brief",
        audio_length="shorter",
    )
    assert kinds(evs) == ["thinking", "step", "step_done", "build", "reply", "state"]
    assert evs[1]["kind"] == "build" and evs[1]["text"] == "Starting an audio overview"
    assert evs[2]["ok"]
    build = evs[3]
    assert build["kind"] == "audio" and build["title"]
    (row,) = await _rows("SELECT id, kind, state, audio FROM sessions")
    assert str(row["id"]) == build["id"] and row["kind"] == "audio"
    assert row["state"] == "preparing" and row["audio"]["format"] == "brief"
    assert len(await _rows("SELECT id FROM jobs WHERE kind = 'prep'")) == 1
    assert evs[4]["text"] == loop.MAKING_IT


async def test_a_read_only_copy_answers_but_refuses_every_tool_that_writes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, web: None
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    await _rows("UPDATE collections SET read_only = true WHERE id = :c RETURNING id", c=cid)
    model.answers(
        calls(
            ("start_build", {"kind": "mindmap", "title": "Reefs"}),
            ("add_sources", {"urls": ["https://kelp.example/kelp"]}),
        ),
        says("This is a read-only copy; start a collection of your own to add or make things."),
    )
    evs = await _say(client, cid, "read https://example.com/reefs and build a mind map")
    refused = [e for e in evs if e["t"] == "step_done"]
    assert len(refused) == 3 and not any(e["ok"] for e in refused)
    for e in refused:
        assert "read-only copy" in e["text"], e
    assert "source" not in kinds(evs) and "build" not in kinds(evs)
    assert "read-only copy" in model.said_to(0, "system")
    tool_said = [m["content"] for m in model.bodies[1]["messages"] if m["role"] == "tool"]
    assert tool_said[0].startswith("refused: This is a read-only copy")
    assert len((await client.get(f"/api/collections/{cid}/sources")).json()) == 1
    assert await _rows("SELECT id FROM mindmaps") == []

    # Asking is allowed.
    model.answers(says("Polyps [1]."))
    evs = await _command(client, cid, "ask", "Who builds reefs?")
    assert evs[-1]["t"] == "reply" and evs[-1]["text"] == "Polyps [1]."
    # And the makers refuse, saying why.
    evs = await _command(client, cid, "notes")
    assert kinds(evs) == ["step", "step_done", "reply"]
    assert not evs[1]["ok"] and "read-only copy" in evs[2]["text"]


async def test_out_of_credit_mid_turn_is_a_sentence_and_what_was_read_stays(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, web: None
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    model.answers(fails(402, "Insufficient credits"))
    evs = await _say(client, cid, "https://example.com/reefs")
    assert kinds(evs) == ["step", "step_done", "source", "thinking", "reply", "state"]
    assert evs[4]["text"] == loop.OUT_OF_CREDIT
    assert evs[5]["ready"]
    assert (await _history(client, cid))[-1]["text"] == loop.OUT_OF_CREDIT


async def test_with_no_ai_key_the_agent_says_how_to_add_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyless = Ai("http://ai.test/v1", "")
    monkeypatch.setattr(client_module, "ai", lambda: keyless)
    cid = await _collection(client)
    evs = await _say(client, cid, "hello")
    assert kinds(evs) == ["reply", "state"]
    assert "OPENNOTEBOOK_AI_KEY" in evs[0]["text"]


async def test_nobody_else_can_chat_in_a_collection(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    them = await other_person(client, "them@test")
    c = f"/api/collections/{cid}/chat"
    for r in [
        await client.post(c, json={"text": "hi"}, headers=them),
        await client.post(f"{c}/commands", json={"name": "help"}, headers=them),
        await client.post(f"{c}/commands", json={"name": "clear"}, headers=them),
        await client.get(c, headers=them),
        await client.post(f"/api/collections/{uuid.uuid4()}/chat", json={"text": "hi"}),
    ]:
        assert r.status_code == 404, r.text
        assert r.json()["detail"].startswith("That collection")
    assert await _rows("SELECT id FROM chat_messages") == []


async def test_a_blank_message_is_refused_in_a_sentence(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/chat", json={"text": "   "})
    assert r.status_code == 422
    assert r.json()["detail"] == "The message is empty. Write something, then send it."


async def test_help_unknown_and_clear_are_answered_by_the_server(client: AsyncClient) -> None:
    listed = (await client.get("/api/commands")).json()
    assert [c["name"] for c in listed] == [c.name for c in commands.COMMANDS]
    cid = await _collection(client)
    evs = await _command(client, cid, "help")
    assert evs == [{"t": "reply", "text": commands.help_text()}]
    evs = await _command(client, cid, "nope", "x")
    assert evs[0]["text"] == "There is no /nope command. Type / to see the ones there are."
    evs = await _command(client, cid, "ask")
    assert evs[0]["text"] == "Ask what? Type the question after /ask."
    evs = await _command(client, cid, "search")
    assert evs[0]["text"] == "What should I search? Type the topic after /search."
    evs = await _command(client, cid, "mindmap", "reefs")
    assert evs[0]["text"].startswith("I make a mind map from your sources, and there are none yet.")
    kept = await _history(client, cid)
    assert [m["text"] for m in kept if m["role"] == "user"] == [
        "/help",
        "/nope x",
        "/ask",
        "/search",
        "/mindmap reefs",
    ]
    assert await _command(client, cid, "clear") == [{"t": "cleared"}]
    assert await _history(client, cid) == []


async def test_ask_from_a_map_keeps_the_question_as_it_was_said(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(says("Polyps [1]."))
    evs = await _command(client, cid, "ask", "Polyps", text="Tell me about polyps")
    assert kinds(evs) == ["step", "step_done", "reply"]
    said, answered = await _history(client, cid)
    assert said["text"] == "Tell me about polyps"
    assert answered["steps"][0]["status"] == "ok" and answered["citations"]


async def test_a_maker_command_makes_it_on_the_server(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    evs = await _command(client, cid, "slides", "Reef basics", style="professional")
    assert kinds(evs) == ["step", "step_done", "build", "reply"]
    assert evs[0]["detail"] == "Reef basics"
    (row,) = await _rows("SELECT id, kind, title, style FROM sessions")
    assert row["kind"] == "slides" and row["title"] == "Reef basics"
    assert row["style"] == "professional"
    assert evs[3]["text"] == (
        "Making narrated slides from your source. It appears in the Studio tab and takes a "
        "few minutes."
    )


async def test_a_research_command_goes_to_the_agent_with_the_web_on(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    model.answers(says("Which part of reefs?"))
    evs = await _command(client, cid, "research", "coral reefs")
    assert evs[-2]["text"] == "Which part of reefs?"
    assert model.said_to(0, "user") == "/research coral reefs"
    assert "deep_research" in {t["function"]["name"] for t in model.bodies[0]["tools"]}


async def test_web_search_returns_hits(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model: Model = install(monkeypatch)
    # The person exists before the call that charges them.
    await client.get("/api/commands")
    model.answers(found("https://example.com/reefs", "https://kelp.example/kelp"))
    r = await client.post("/api/search", json={"query": "reefs"})
    assert r.status_code == 200, r.text
    hits = r.json()
    assert [h["url"] for h in hits] == ["https://example.com/reefs", "https://kelp.example/kelp"]
    assert hits[0]["snippet"] == "Reefs and kelp."
    assert model.bodies[0]["model"] == "perplexity/sonar"
    assert [r["kind"] for r in await spent()] == ["web_search"]
    model.answers(fails(402, "Insufficient credits"))
    r = await client.post("/api/search", json={"query": "reefs"})
    assert r.status_code == 402
    assert r.json()["detail"].startswith("The AI account is out of credit")
