"""A stand-in for the AI endpoint, for route tests: the studio's `ai()`
answers from a queue of replies, and every request it was sent is kept."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.ai import client
from opennotebook.ai.client import Ai
from opennotebook.db.session import engine

Reply = Callable[[httpx2.Request], httpx2.Response]

# Every model the studio defaults to, priced, so a call is charged and an
# estimate has a price.
PRICE_IN = 0.000_001
PRICE_OUT = 0.000_004
MODELS = {
    "data": [
        {
            "id": "google/gemini-2.5-flash-lite",
            "pricing": {"prompt": str(PRICE_IN), "completion": str(PRICE_OUT)},
        }
    ]
}


def says(text: str, finish: str = "stop") -> Reply:
    body = {
        "model": "google/gemini-2.5-flash-lite",
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    return lambda _: httpx2.Response(200, json=body)


def fails(status: int, message: str) -> Reply:
    return lambda _: httpx2.Response(status, json={"error": {"message": message}})


class Model:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.bodies: list[dict[str, Any]] = []

    def answers(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json=MODELS)
        self.bodies.append(json.loads(request.content))
        assert self.replies, "the model was asked more often than the test expected"
        return self.replies.pop(0)(request)

    def said_to(self, i: int, role: str) -> str:
        """What request `i` said as `role`."""
        return next(m["content"] for m in self.bodies[i]["messages"] if m["role"] == role)


def install(monkeypatch: pytest.MonkeyPatch) -> Model:
    """Point the studio's model client at a fresh `Model`."""
    model = Model()
    ai = Ai(
        "http://ai.test/v1",
        "k",
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(model)),
        backoff=0,
    )
    monkeypatch.setattr(client, "ai", lambda: ai)
    return model


async def spent() -> list[dict[str, Any]]:
    """Every row of the spend ledger."""
    async with engine().begin() as c:
        rows = await c.execute(
            text("SELECT kind, collection_id, job_id, model, cost_usd, priced_by FROM usage_events")
        )
        return [dict(r) for r in rows.mappings()]


async def add_note(c: AsyncClient, cid: str, note: str, title: str = "") -> str:
    """Add a note source and return its name."""
    r = await c.post(
        f"/api/collections/{cid}/sources", json={"kind": "text", "text": note, "title": title}
    )
    assert r.status_code == 201, r.text
    return r.json()[0]["source"]["name"]


async def run_work() -> None:
    """Run every job waiting on the work queue, as the worker would: maps,
    notes and research the test started."""
    from opennotebook.jobs.app import WORK_QUEUE, app

    async with app.open_async():
        await app.run_worker_async(
            queues=[WORK_QUEUE],
            wait=False,
            install_signal_handlers=False,
            listen_notify=False,
            concurrency=1,
        )


async def made(
    c: AsyncClient, path: str, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Ask for a map or notes at `path`, let the worker make it, and return
    its job as it ended and the output as it then reads, or None when its
    making failed and it was removed."""
    r = await c.post(path, json=body or {})
    assert r.status_code == 202, r.text
    started = r.json()
    row = started.get("mindmap") or started.get("notes")
    assert row["state"] == "making" and row["job_id"] == started["job"]["id"]
    await run_work()
    job = (await c.get(f"/api/jobs/{started['job']['id']}")).json()
    got = await c.get(f"{path}/{row['id']}")
    return job, (got.json() if got.status_code == 200 else None)
