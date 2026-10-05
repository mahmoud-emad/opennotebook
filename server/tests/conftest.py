"""Tests run against a real Postgres: the database in TEST_DATABASE_URL,
migrated from empty at the start of the run and emptied after every test."""

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

# Before anything reads the settings: the app talks to the test database.
if not os.environ.get("TEST_DATABASE_URL"):
    pytest.exit("Set TEST_DATABASE_URL to an empty Postgres database (make check does).", 2)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["OPENNOTEBOOK_AUTH"] = "local"
# No test reaches a real AI endpoint; one that tries fails at once.
os.environ["OPENNOTEBOOK_AI_BASE_URL"] = "http://ai.invalid/v1"

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from opennotebook.config import settings
from opennotebook.db.models import Base
from opennotebook.db.session import engine
from opennotebook.main import create_app

HERE = os.path.dirname(__file__)


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    cfg = Config(os.path.join(HERE, "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings().sqlalchemy_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
async def model_down(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Adding a source names the collection and designs its cover in the
    background. Unless a test says otherwise, the model those ask is down and
    says so at once, so naming falls back to the heuristic and the cover is
    kept, without a wait."""
    import httpx2

    from opennotebook.ai.client import Ai
    from opennotebook.domain import covers, naming

    http = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(503, json={"error": {"message": "down"}})
        )
    )
    down = Ai("http://ai.test/v1", "k", http=http, retries=0)
    monkeypatch.setattr(naming, "ai", lambda: down)
    monkeypatch.setattr(covers, "ai", lambda: down)
    yield
    await http.aclose()


@pytest.fixture(autouse=True)
async def empty_tables(model_down: None) -> AsyncIterator[None]:
    yield
    # Background work a test started finishes before its rows go.
    from opennotebook.domain import refresh

    await refresh.settle()
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine().begin() as c:
        await c.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def other_person(client: AsyncClient, email: str) -> dict[str, str]:
    """Headers that act as a second person, through an API key of theirs."""
    from opennotebook.auth import key_hash, new_key

    key = new_key()
    async with engine().begin() as c:
        uid = (
            await c.execute(
                text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}
            )
        ).scalar_one()
        await c.execute(
            text("INSERT INTO api_keys (owner_id, prefix, hash) VALUES (:o, :p, :h)"),
            {"o": uid, "p": key[:12], "h": key_hash(key)},
        )
    return {"Authorization": f"Bearer {key}"}
