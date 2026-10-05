"""Ported from opennotebook_ai/src/tests.rs, against a mock endpoint."""

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from opennotebook.ai import ledger
from opennotebook.ai.client import Ai, Done, TextDelta
from opennotebook.ai.errors import AiError, Kind
from opennotebook.db.session import engine

Reply = Callable[[httpx2.Request], httpx2.Response]


class Mock:
    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json=MODELS)
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0)(request)

    def ai(self) -> Ai:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        return Ai("http://ai.test/v1", "k", http=http, backoff=0)


MODELS = {"data": [{"id": "m/priced", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}


def ok(body: dict[str, Any]) -> Reply:
    return lambda _: httpx2.Response(200, json=body)


def status(code: int, body: Any) -> Reply:
    return lambda _: httpx2.Response(code, json=body)


def sse(*chunks: Any) -> Reply:
    lines = "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)
    return lambda _: httpx2.Response(
        200, content=lines.encode(), headers={"content-type": "text/event-stream"}
    )


ANSWER = {
    "model": "m/priced",
    "choices": [{"message": {"content": "Reefs."}, "finish_reason": "end_turn"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.0042},
}


@pytest.fixture
async def owner() -> uuid.UUID:
    async with engine().begin() as c:
        return (
            await c.execute(text("INSERT INTO users (email) VALUES ('ai@test') RETURNING id"))
        ).scalar_one()


async def _events() -> list[dict[str, Any]]:
    async with engine().begin() as c:
        rows = await c.execute(
            text("SELECT model, input_tokens, output_tokens, cost_usd, priced_by FROM usage_events")
        )
        return [dict(r) for r in rows.mappings()]


async def test_a_completion_reads_text_usage_cost_and_finish(owner: uuid.UUID) -> None:
    mock = Mock(ok(ANSWER))
    async with ledger.spending(owner, "chat") as spend:
        done = await mock.ai().complete("m/priced", [{"role": "user", "content": "Hi"}])
    assert done.text == "Reefs." and done.finish_reason == "stop" and done.model == "m/priced"
    assert done.usage is not None and done.usage.cost_usd == 0.0042
    # Cost is asked for in the body.
    assert mock.bodies[0]["usage"] == {"include": True}
    assert str(spend.total_usd) == "0.0042" and spend.known
    (row,) = await _events()
    assert row["priced_by"] == "provider" and row["input_tokens"] == 10


async def test_a_call_without_a_reported_cost_is_priced_or_marked_unpriced(
    owner: uuid.UUID,
) -> None:
    no_cost = {**ANSWER, "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
    mock = Mock(ok(no_cost), ok({**no_cost, "model": "m/other"}))
    async with ledger.spending(owner, "notes") as spend:
        await mock.ai().complete("m/priced", [])
        await mock.ai().complete("m/unlisted", [])
    by = sorted((r["priced_by"], r["cost_usd"]) for r in await _events())
    assert by[0][0] == "catalogue" and float(by[0][1]) == pytest.approx(0.002)
    assert by[1] == ("unpriced", None)
    assert not spend.known


async def test_tool_calls_round_trip(owner: uuid.UUID) -> None:
    reply = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "search", "arguments": '{"q": "reefs"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    mock = Mock(ok(reply))
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    async with ledger.spending(owner, "chat"):
        done = await mock.ai().complete("m/priced", [], tools=tools, tool_choice="auto")
    assert done.tool_calls[0].name == "search" and done.tool_calls[0].arguments == {"q": "reefs"}
    assert mock.bodies[0]["tools"] == tools and mock.bodies[0]["tool_choice"] == "auto"


async def test_out_of_credit_is_its_own_error_and_is_not_retried(owner: uuid.UUID) -> None:
    mock = Mock(status(402, {"error": {"message": "Insufficient credits"}}), ok(ANSWER))
    with pytest.raises(AiError) as e:
        async with ledger.spending(owner, "chat"):
            await mock.ai().complete("m/priced", [])
    assert e.value.kind == Kind.QUOTA and len(mock.bodies) == 1


async def test_an_unavailable_upstream_is_retried(owner: uuid.UUID) -> None:
    mock = Mock(status(503, {"error": {"message": "down"}}), ok(ANSWER))
    async with ledger.spending(owner, "chat"):
        assert (await mock.ai().complete("m/priced", [])).text == "Reefs."
    assert len(mock.bodies) == 2


async def test_an_error_inside_a_200_is_an_error(owner: uuid.UUID) -> None:
    mock = Mock(ok({"error": {"message": "insufficient_quota", "code": "insufficient_quota"}}))
    with pytest.raises(AiError) as e:
        async with ledger.spending(owner, "chat"):
            await mock.ai().complete("m/priced", [])
    assert e.value.kind == Kind.QUOTA


async def test_an_unreachable_server_is_unavailable(owner: uuid.UUID) -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    mock = Mock(refuse, refuse, refuse)
    with pytest.raises(AiError) as e:
        async with ledger.spending(owner, "chat"):
            await mock.ai().complete("m/priced", [])
    assert e.value.kind == Kind.UNAVAILABLE and len(mock.bodies) == 3
    assert e.value.sentence.startswith("The AI provider is not answering")


async def test_a_stream_yields_text_then_done_with_tool_calls(owner: uuid.UUID) -> None:
    mock = Mock(
        sse(
            ": keep-alive",
            {"model": "m/priced", "choices": [{"delta": {"content": "Re"}}]},
            {"choices": [{"delta": {"content": "efs"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "ask", "arguments": '{"q"'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "x"}'}}]}}
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.001}},
            "[DONE]",
        )
    )
    async with ledger.spending(owner, "chat") as spend:
        events = [e async for e in mock.ai().stream("m/priced", [])]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Re", "efs"]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.completion.text == "Reefs" and done.completion.finish_reason == "tool_calls"
    assert done.completion.tool_calls[0].arguments == {"q": "x"}
    assert mock.bodies[0]["stream_options"] == {"include_usage": True}
    assert spend.calls == 1 and str(spend.total_usd) == "0.001"


async def test_an_audio_models_transcript_is_its_text(owner: uuid.UUID) -> None:
    reply = {"choices": [{"message": {"content": None, "audio": {"transcript": "Hello."}}}]}
    async with ledger.spending(owner, "voice"):
        assert (await Mock(ok(reply)).ai().complete("m/priced", [])).text == "Hello."


async def test_a_json_schema_and_temperature_reach_the_body(owner: uuid.UUID) -> None:
    mock = Mock(ok(ANSWER))
    async with ledger.spending(owner, "map"):
        await mock.ai().complete(
            "m/priced", [], temperature=0.2, json_schema=("map", {"type": "object"})
        )
    body = mock.bodies[0]
    assert body["temperature"] == 0.2
    assert body["response_format"]["json_schema"]["name"] == "map"
