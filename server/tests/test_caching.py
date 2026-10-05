"""What a process keeps rather than reads again: a person's settings for a few
seconds, dropped the moment they change one; the price list, answered from
what was read while a fresh one is fetched; and one HTTP client per speech
service instead of one per line."""

import asyncio
import uuid
from typing import Any

import httpx
import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import speech
from opennotebook.ai import prices
from opennotebook.ai.prices import Catalogue
from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import settings as st
from opennotebook.speech import microsoft
from opennotebook.speech import provider as providers
from tests.queries import recorded


@pytest.fixture
def kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings kept as the studio keeps them, which the other tests turn off."""
    monkeypatch.setattr(st, "CACHE_SECONDS", 5.0)


async def _me(client: AsyncClient) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/me")).json()["id"])


async def _language(owner: uuid.UUID) -> tuple[str, int]:
    """The language in force for `owner`, and how many queries it took."""
    async with sessionmaker()() as s:
        with recorded() as q:
            v = await st.value(s, owner, st.LANGUAGE_KEY)
    return v, len(q)


async def test_settings_are_kept_and_dropped_when_one_changes(
    client: AsyncClient, kept: None
) -> None:
    me = await _me(client)
    first, asked = await _language(me)
    assert first == "English" and asked > 0
    again, asked = await _language(me)
    assert (again, asked) == ("English", 0), "kept: no query"
    # Every setting of the person comes from what was kept.
    async with sessionmaker()() as s:
        with recorded() as q:
            await st.value(s, me, st.SLIDE_COUNT_KEY)
            await st.value_of_each(s, {me}, st.COVERS_KEY)
    assert len(q) == 0

    r = await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": "French"})
    assert r.status_code == 200, r.text
    assert (await _language(me))[0] == "French", "a change is seen at once"
    r = await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": ""})
    assert (await _language(me))[0] == "English"


async def test_a_change_made_elsewhere_is_seen_once_what_was_kept_is_old(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(st, "CACHE_SECONDS", 0.2)
    me = await _me(client)
    assert (await _language(me))[0] == "English"
    # Another process (the worker, another api) writes the row.
    async with engine().begin() as c:
        await c.execute(
            text("INSERT INTO user_settings (owner_id, key, value) VALUES (:o, :k, 'Spanish')"),
            {"o": me, "k": st.LANGUAGE_KEY},
        )
    assert (await _language(me))[0] == "English", "kept a moment longer"
    await asyncio.sleep(0.25)
    assert (await _language(me))[0] == "Spanish"


async def test_an_instance_change_is_seen_at_once(client: AsyncClient, kept: None) -> None:
    me = await _me(client)
    async with sessionmaker()() as s:
        before = await st.value(s, me, st.CHAT_MODEL_KEY)
    assert before != "acme/model-1"
    # The models are set for everyone, here by the studio's local owner.
    r = await client.patch(f"/api/settings/{st.CHAT_MODEL_KEY}", json={"value": "acme/model-1"})
    assert r.status_code == 200, r.text
    async with sessionmaker()() as s:
        assert await st.value(s, me, st.CHAT_MODEL_KEY) == "acme/model-1"


# ── the price list ────────────────────────────────────────────────────────────

MODELS = {"data": [{"id": "a/one", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
LATER = {"data": [{"id": "a/one", "pricing": {"prompt": "0.000003", "completion": "0.000004"}}]}


class Endpoint:
    """A `/models` that counts its calls, answers `body`, and can be held."""

    def __init__(self) -> None:
        self.calls = 0
        self.body: Any = MODELS
        self.status = 200
        self.gate: asyncio.Event | None = None

    async def __call__(self, _: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return httpx2.Response(self.status, json=self.body)


def _catalogue(e: Endpoint) -> Catalogue:
    return Catalogue(
        "http://ai.test/v1", "k", httpx2.AsyncClient(transport=httpx2.MockTransport(e))
    )


async def test_callers_at_once_share_one_read() -> None:
    e = Endpoint()
    e.gate = asyncio.Event()
    c = _catalogue(e)
    waiting = [asyncio.create_task(c.price("a/one")) for _ in range(5)]
    await asyncio.sleep(0.01)
    e.gate.set()
    got = await asyncio.gather(*waiting)
    assert e.calls == 1
    assert all(p is not None and p.input_per_token == 0.000001 for p in got)


async def test_an_old_list_is_answered_at_once_while_a_new_one_is_read() -> None:
    e = Endpoint()
    c = _catalogue(e)
    assert await c.price("a/one") is not None
    # Ten minutes on, and the endpoint has new prices but is slow to say so.
    c._read_at -= prices.TTL_SECONDS + 1  # pyright: ignore[reportPrivateUsage]
    e.body, e.gate = LATER, asyncio.Event()
    async with asyncio.timeout(1):
        old = await c.price("a/one")
    assert old is not None and old.input_per_token == 0.000001, "nobody waits"
    await asyncio.sleep(0.01)
    assert e.calls == 2, "the new list is being read meanwhile"
    e.gate.set()
    await asyncio.sleep(0.01)
    new = await c.price("a/one")
    assert new is not None and new.input_per_token == 0.000003
    assert e.calls == 2


async def test_an_endpoint_that_is_down_is_not_asked_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e = Endpoint()
    e.status = 503
    c = _catalogue(e)
    for _ in range(3):
        assert await c.price("a/one") is None
    assert e.calls == 1, "remembered as down"
    # Once the wait is over, it is asked again.
    monkeypatch.setattr(prices, "RETRY_SECONDS", 0.0)
    e.status = 200
    assert await c.price("a/one") is not None
    assert e.calls == 2


# ── speech ────────────────────────────────────────────────────────────────────


async def test_every_line_goes_through_one_kept_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(providers.PROVIDER_KEY, "openai")
    one, two = speech.speech(), speech.speech()
    assert one._http is two._http is speech.shared_http()  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv(providers.PROVIDER_KEY, "azure")
    s = speech.speech()
    assert isinstance(s, microsoft.MicrosoftSpeech)
    assert s._azure_http is microsoft.azure_http()  # pyright: ignore[reportPrivateUsage]

    kept = (speech.shared_http(), microsoft.azure_http())
    await speech.close_shared()
    assert all(c.is_closed for c in kept)
    assert speech.shared_http() is not kept[0], "made again on next use"
    await speech.close_shared()


async def test_an_azure_line_reuses_the_kept_client(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[httpx.AsyncClient] = []
    asked: list[str] = []

    def azure(r: httpx.Request) -> httpx.Response:
        asked.append(str(r.url))
        from opennotebook.speech.wav import ramp_wav

        return httpx.Response(200, content=ramp_wav(24_000, 1, 120))

    def new() -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.MockTransport(azure))
        made.append(c)
        return c

    monkeypatch.setattr(microsoft, "new_http", new)
    monkeypatch.setenv(providers.PROVIDER_KEY, "azure")
    monkeypatch.setenv(microsoft.AZURE_KEY_KEY, "k")
    monkeypatch.setenv(microsoft.AZURE_REGION_KEY, "eastus")
    microsoft.azure_http.cache_clear()
    for _ in range(3):
        await speech.speech().synthesize("Hi.", "en-US-AvaMultilingualNeural")
    assert len(asked) == 3 and len(made) == 1, "three lines, one client"
    assert not made[0].is_closed, "kept open between lines"
    await speech.close_shared()
    assert made[0].is_closed
