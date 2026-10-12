"""The FastAPI app: every router, the readable-error layer, CORS for the
web app's dev server, and a clean stop."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from opennotebook import errors
from opennotebook.api import (
    account,
    ask,
    chat,
    collections,
    mcp,
    media,
    mindmaps,
    notes,
    providers,
    rpc,
    sessions,
    settings,
    shares,
    sources,
    video,
)
from opennotebook.config import settings as config
from opennotebook.shutdown import close_all


def _operation_id(route: APIRoute) -> str:
    # The generated client names each call after its handler: listCollections.
    return route.name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # The MCP tools' sessions run as long as the app does.
    async with app.state.mcp.session_manager.run():
        yield
    # Running chat turns get a moment to finish and keep what they said;
    # then every connection the process holds is closed.
    await close_all()


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="OpenNotebook",
        version="0.1.0",
        description="Collections of sources, and the decks, audio overviews, mind maps and "
        "notes made from them. Everything the web app does goes through this API.",
        generate_unique_id_function=_operation_id,
    )
    errors.install(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config().web_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (
        account,
        providers,
        collections,
        sources,
        ask,
        sessions,
        media,
        mindmaps,
        notes,
        chat,
        settings,
        shares,
        video,
    ):
        app.include_router(module.router)
    # DEPRECATED: the old JSON-RPC API, for one release after cutover; delete
    # this line with api/rpc.py, api/rpc_methods.py and api/rpc_openrpc/.
    app.include_router(rpc.router)
    # The tools for agents, at /api/mcp, calling the routes above.
    app.state.mcp = mcp.make(app)
    return app


app = create_app()
