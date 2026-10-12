"""The studio as an MCP server, for agents: at `/api/mcp`, over streamable HTTP.

Each tool is a call to the studio's own REST API, made in-process with the
caller's `Authorization` header passed on. So an agent signs in with an API
key as any client does (in `local` mode, without one, as the local owner),
sees only its owner's collections, is refused for the same reasons with the
same sentences, and every model call it causes is charged to its owner.

Connect an MCP client to `http(s)://<studio>/api/mcp` with the header
`Authorization: Bearer <api key>`.
"""

from typing import Any

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

INSTRUCTIONS = (
    "OpenNotebook keeps collections of sources (documents, web pages, videos, notes) and "
    "answers questions from them with citations. List the collections first; ask questions "
    "of one by its id; add links or notes to give it more to read."
)


def make(app: FastAPI) -> MCPServer:
    """The tools, calling `app`, and their route `/api/mcp` added to it,
    where the proxy in front already sends the API. Hosts
    are not checked here: who may call is decided by the API key, as for
    the REST API. The app's lifespan runs `server.session_manager`."""
    server = MCPServer(name="OpenNotebook", instructions=INSTRUCTIONS)

    async def call(ctx: Context, method: str, path: str, body: Any = None) -> Any:
        """One REST call as the caller, or a `ToolError` with the studio's
        own sentence for a refusal."""
        headers = {}
        if (auth := (ctx.headers or {}).get("authorization")) is not None:
            headers["Authorization"] = auth
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio", timeout=300
        ) as http:
            r = await http.request(method, f"/api{path}", json=body, headers=headers)
        if r.status_code >= 400:
            try:
                got: Any = r.json()
            except ValueError:
                got = None
            said = got.get("detail") if isinstance(got, dict) else None
            raise ToolError(
                said if isinstance(said, str) and said else "The studio refused that request."
            )
        return r.json() if r.content else None

    @server.tool()
    async def list_collections(ctx: Context) -> list[dict[str, Any]]:
        """Your collections, most recently updated first: each one's id,
        title and how many sources it holds."""
        got = await call(ctx, "GET", "/collections")
        return [
            {"id": c["id"], "title": c["title"], "sources": c.get("sources", 0)} for c in got or []
        ]

    @server.tool()
    async def create_collection(ctx: Context, title: str) -> dict[str, Any]:
        """Start an empty collection with this title; returns its id."""
        c = await call(ctx, "POST", "/collections", {"title": title})
        return {"id": c["id"], "title": c["title"]}

    @server.tool()
    async def list_sources(ctx: Context, collection_id: str) -> list[dict[str, Any]]:
        """The sources in a collection: each one's name, title, kind and link."""
        got = await call(ctx, "GET", f"/collections/{collection_id}/sources")
        return [
            {"name": s["name"], "title": s["title"], "kind": s["kind"], "url": s.get("url", "")}
            for s in got or []
        ]

    @server.tool()
    async def read_source(ctx: Context, collection_id: str, name: str) -> dict[str, Any]:
        """One source's whole text, by its name from `list_sources`."""
        s = await call(ctx, "GET", f"/collections/{collection_id}/sources/{name}")
        return {"name": s["name"], "title": s["title"], "text": s["text"]}

    @server.tool()
    async def ask(ctx: Context, collection_id: str, question: str) -> dict[str, Any]:
        """Answer a question from a collection's sources, with numbered
        citations of the passages it rests on."""
        return await call(ctx, "POST", f"/collections/{collection_id}/ask", {"question": question})

    @server.tool()
    async def add_links(ctx: Context, collection_id: str, urls: list[str]) -> list[dict[str, Any]]:
        """Read web pages or YouTube videos into a collection, up to eight at
        a time. One result per link, saying why when one could not be read."""
        got = await call(
            ctx, "POST", f"/collections/{collection_id}/sources", {"kind": "urls", "urls": urls}
        )
        return [
            {
                "url": r["url"],
                "ok": r["ok"],
                "error": r["error"],
                "title": (r.get("source") or {}).get("title", ""),
            }
            for r in got or []
        ]

    @server.tool()
    async def add_note(
        ctx: Context, collection_id: str, text: str, title: str = ""
    ) -> dict[str, Any]:
        """Add text (plain or Markdown) to a collection as a source."""
        [r] = await call(
            ctx,
            "POST",
            f"/collections/{collection_id}/sources",
            {"kind": "text", "text": text, "title": title},
        )
        return {"ok": r["ok"], "title": (r.get("source") or {}).get("title", "")}

    asgi = server.streamable_http_app(
        streamable_http_path="/api/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # The route itself, not a mount: a mount would answer `/api/mcp` with
    # a redirect to `/api/mcp/`, which not every client follows.
    app.router.routes.extend(asgi.routes)
    return server
