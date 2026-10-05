"""The FastAPI app: every router, the readable-error layer, and CORS for the
web app's dev server."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from opennotebook import errors
from opennotebook.api import (
    account,
    ask,
    chat,
    collections,
    media,
    mindmaps,
    notes,
    sessions,
    settings,
    shares,
    sources,
)
from opennotebook.config import settings as config


def _operation_id(route: APIRoute) -> str:
    # The generated client names each call after its handler: listCollections.
    return route.name


def create_app() -> FastAPI:
    app = FastAPI(
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
    ):
        app.include_router(module.router)
    return app


app = create_app()
