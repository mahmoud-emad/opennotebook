"""The MCP tools at /api/mcp: each one a REST call made as its caller."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from opennotebook.config import settings as config
from opennotebook.main import create_app
from tests.conftest import other_person

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}


@pytest.fixture
async def studio() -> AsyncIterator[AsyncClient]:
    """The app with its MCP sessions running, as its lifespan runs them: in
    a task of their own, which starts and stops them."""
    app = create_app()
    up, down = asyncio.Event(), asyncio.Event()

    async def sessions() -> None:
        async with app.state.mcp.session_manager.run():
            up.set()
            await down.wait()

    task = asyncio.create_task(sessions())
    await up.wait()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    down.set()
    await task


async def tool(
    c: AsyncClient, tool_name: str, headers: dict[str, str] | None = None, **arguments: Any
) -> dict[str, Any]:
    r = await c.post(
        "/api/mcp",
        headers=HEADERS | (headers or {}),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def test_the_tools_are_listed(studio: AsyncClient) -> None:
    r = await studio.post(
        "/api/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "list_collections",
        "create_collection",
        "list_sources",
        "read_source",
        "ask",
        "add_links",
        "add_note",
    }


async def test_an_agent_makes_a_collection_and_reads_it_back(studio: AsyncClient) -> None:
    made = (await tool(studio, "create_collection", title="Reefs"))["structuredContent"]
    cid = made["id"]
    added = await tool(studio, "add_note", collection_id=cid, text="Coral is an animal.")
    assert added["structuredContent"]["ok"] is True
    listed = (await tool(studio, "list_collections"))["structuredContent"]["result"]
    assert [(c["id"], c["title"], c["sources"]) for c in listed] == [(cid, "Reefs", 1)]
    [src] = (await tool(studio, "list_sources", collection_id=cid))["structuredContent"]["result"]
    read = await tool(studio, "read_source", collection_id=cid, name=src["name"])
    assert read["structuredContent"]["text"] == "Coral is an animal."


async def test_a_refusal_is_the_studios_own_sentence(studio: AsyncClient) -> None:
    cid = (await tool(studio, "create_collection", title="Empty"))["structuredContent"]["id"]
    got = await tool(studio, "ask", collection_id=cid, question="What is coral?")
    assert got["isError"] is True
    # The SDK names the tool; the studio's sentence follows as it is.
    assert got["content"][0]["text"] == (
        "Error executing tool ask: Add a source first: a link, a note, or a topic to research."
    )


async def test_an_agent_sees_only_its_keys_collections(
    studio: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await tool(studio, "create_collection", title="The owner's")
    monkeypatch.setattr(config(), "auth", "keys")
    them = await other_person(studio, "agent@example.com")
    await tool(studio, "create_collection", headers=them, title="The agent's")
    listed = (await tool(studio, "list_collections", headers=them))["structuredContent"]["result"]
    assert [c["title"] for c in listed] == ["The agent's"]
    # Without a key, in keys mode, nothing is answered.
    got = await tool(studio, "list_collections")
    assert got["isError"] is True
