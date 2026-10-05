"""Tests run against a real Postgres: the database in TEST_DATABASE_URL,
migrated from empty at the start of the run and emptied after every test."""

import os
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

# Before anything reads the settings: the app talks to the test database.
if not os.environ.get("TEST_DATABASE_URL"):
    pytest.exit("Set TEST_DATABASE_URL to an empty Postgres database (make check does).", 2)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["OPENNOTEBOOK_AUTH"] = "local"
# No test reaches a real AI endpoint; one that tries fails at once.
os.environ["OPENNOTEBOOK_AI_BASE_URL"] = "http://ai.invalid/v1"
# Lines are read aloud by the OpenAI-compatible client, which the tests fake;
# the Microsoft voices' own tests choose them where they need them.
os.environ["OPENNOTEBOOK_TTS_PROVIDER"] = "openai"

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
    # `heads`: work on separate branches can each add a revision of its own.
    command.upgrade(cfg, "heads")


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
def no_internet_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Microsoft's voices are services on the internet: a test that reaches
    for them without a fake of its own fails at once instead of calling out."""
    import httpx

    from opennotebook import speech
    from opennotebook.speech import microsoft

    async def offline(text: str, voice: str) -> AsyncIterator[Mapping[str, Any]]:
        raise OSError("tests do not reach Microsoft's voice service")
        yield {}

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tests do not reach Azure Speech")

    monkeypatch.setattr(microsoft, "edge_stream", offline)
    monkeypatch.setattr(
        microsoft, "new_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(down))
    )
    # The kept clients are made afresh in each test, from its own stand-ins.
    microsoft.azure_http.cache_clear()
    speech.shared_http.cache_clear()


@pytest.fixture(autouse=True)
def settings_read_fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests write settings straight into their tables, so unless a test says
    otherwise every read goes to the database, as the worker's would after
    `CACHE_SECONDS`; the tests of the cache turn it on."""
    from opennotebook.domain import settings as st

    monkeypatch.setattr(st, "CACHE_SECONDS", 0.0)
    st.forget()
    yield
    st.forget()


@pytest.fixture(autouse=True)
async def empty_tables(model_down: None) -> AsyncIterator[None]:
    yield
    # Background work a test started finishes before its rows go.
    from opennotebook.domain import refresh

    await refresh.settle()
    # The queue too: a job left waiting would run against the next test's rows.
    tables = ", ".join([*(t.name for t in Base.metadata.sorted_tables), "procrastinate_jobs"])
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
