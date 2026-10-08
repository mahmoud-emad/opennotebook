"""Connecting AI providers: where connections come from, how a model names
its provider, what each provider is sent, how a key is tested, and the setup
routes the web app's tour uses."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.ai import connections, secrets
from opennotebook.ai.check import Check, check
from opennotebook.ai.client import Ai, Router
from opennotebook.ai.connections import Connection
from opennotebook.ai.errors import AiError, Kind
from opennotebook.ai.providers import BY_KIND, kind_of_url
from opennotebook.config import settings as config
from opennotebook.db.session import engine
from opennotebook.domain import settings as st
from tests.conftest import other_person

Handler = Callable[[httpx2.Request], httpx2.Response]


def conn(
    kind: str, key: str = "sk-test-key-1234", cid: str | None = None, url: str = ""
) -> Connection:
    p = BY_KIND[kind]
    return Connection(cid or kind, kind, p.label, url or p.base_url, key, "app")


def http(handler: Handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def reply(text_: str = "OK") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "model": "m",
            "choices": [{"message": {"content": text_}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        },
    )


# ── where connections come from ───────────────────────────────────────────────


def test_the_studios_own_key_connects_openrouter_unless_the_address_says_otherwise() -> None:
    [c] = connections.from_env({"OPENNOTEBOOK_AI_KEY": "or-key"})
    assert (c.id, c.kind, c.base_url, c.source) == (
        "openrouter",
        "openrouter",
        "https://openrouter.ai/api/v1",
        "env",
    )
    [c] = connections.from_env(
        {"OPENNOTEBOOK_AI_KEY": "k", "OPENNOTEBOOK_AI_BASE_URL": "https://api.openai.com/v1/"}
    )
    assert (c.kind, c.base_url) == ("openai", "https://api.openai.com/v1")
    [c] = connections.from_env(
        {"OPENNOTEBOOK_AI_KEY": "k", "OPENNOTEBOOK_AI_BASE_URL": "http://gpu.lan:8000/v1"}
    )
    assert c.kind == "custom"


def test_each_providers_own_variable_connects_it() -> None:
    found = connections.from_env(
        {
            "OPENROUTER_API_KEY": "a",
            "OPENAI_API_KEY": "b",
            "ANTHROPIC_API_KEY": "c",
            "OLLAMA_BASE_URL": "http://localhost:11434/v1",
        }
    )
    assert [c.id for c in found] == ["openrouter", "openai", "anthropic", "ollama"]
    # Ollama needs no key.
    assert all(c.usable for c in found)


def test_nothing_set_is_nothing_connected() -> None:
    assert connections.from_env({"OPENNOTEBOOK_AI_BASE_URL": "http://ai.invalid/v1"}) == []


def test_a_connection_never_shows_its_key() -> None:
    assert "sk-test" not in repr(conn("openai"))


def test_an_address_names_its_provider() -> None:
    assert kind_of_url("https://openrouter.ai/api/v1") == "openrouter"
    assert kind_of_url("https://api.anthropic.com/v1") == "anthropic"
    assert kind_of_url("http://localhost:1234/v1") == "custom"


# ── naming a model ────────────────────────────────────────────────────────────


def test_a_model_names_its_connection_or_belongs_to_the_primary() -> None:
    conns = [conn("openrouter"), conn("openai"), conn("ollama", key="")]
    openrouter, openai, ollama = conns
    assert connections.split("anthropic/claude-haiku-4.5", conns) == (
        openrouter,
        "anthropic/claude-haiku-4.5",
    )
    assert connections.split("openai:gpt-5.4", conns) == (openai, "gpt-5.4")
    assert connections.split("ollama:llama3.2:3b", conns) == (ollama, "llama3.2:3b")
    # A colon that names no connection is part of the id.
    assert connections.split("qwen/qwen3:free", conns) == (openrouter, "qwen/qwen3:free")
    assert connections.ref(openrouter, "x/y", conns) == "x/y"
    assert connections.ref(openai, "gpt-5.4", conns) == "openai:gpt-5.4"


def test_with_nothing_connected_a_model_has_no_connection() -> None:
    assert connections.split("anthropic/claude-haiku-4.5", []) == (
        None,
        "anthropic/claude-haiku-4.5",
    )


def test_a_role_takes_the_first_suggestion_the_provider_offers() -> None:
    c = conn("openai")
    assert connections.suggest(c, "text", {"gpt-5-mini", "gpt-4.1-mini"}) == "gpt-5-mini"
    # Its list unknown: the first suggestion.
    assert connections.suggest(c, "text", None) == "gpt-5.4-mini"
    assert connections.suggest(c, "image", None) is None


def test_new_connections_of_one_kind_are_numbered() -> None:
    assert connections.new_id("custom", set()) == "custom"
    assert connections.new_id("custom", {"custom", "custom-2"}) == "custom-3"


# ── keeping a key secret ──────────────────────────────────────────────────────


def test_a_stored_key_is_encrypted_and_read_back() -> None:
    sealed = secrets.seal("sk-secret")
    assert "sk-secret" not in sealed
    assert secrets.unseal(sealed) == "sk-secret"
    assert secrets.seal("") == "" and secrets.unseal("") == ""


def test_a_key_sealed_under_another_secret_reads_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = secrets.seal("sk-secret")
    monkeypatch.setenv(secrets.SECRET_ENV, "another")
    secrets.forget()
    try:
        assert secrets.unseal(sealed) == ""
    finally:
        monkeypatch.undo()
        secrets.forget()


# ── what each provider is sent ───────────────────────────────────────────────


class Seen:
    def __init__(self, answer: str = "OK") -> None:
        self.bodies: list[dict[str, Any]] = []
        self.answer = answer

    def __call__(self, r: httpx2.Request) -> httpx2.Response:
        if r.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        self.bodies.append(json.loads(r.content))
        return reply(self.answer)

    def ai(self, kind: str) -> Ai:
        return Ai("http://ai.test/v1", "k", http=http(self), retries=0, preset=BY_KIND[kind])


async def test_only_openrouter_is_asked_for_its_cost_report() -> None:
    seen = Seen()
    await seen.ai("openrouter").complete("m", [{"role": "user", "content": "hi"}])
    await seen.ai("openai").complete("m", [{"role": "user", "content": "hi"}])
    assert seen.bodies[0]["usage"] == {"include": True}
    assert "usage" not in seen.bodies[1]


async def test_openais_reasoning_models_get_the_length_and_temperature_they_take() -> None:
    seen = Seen()
    ai = seen.ai("openai")
    await ai.complete("gpt-5.4", [], max_tokens=50, temperature=0.2)
    await ai.complete("gpt-4.1-mini", [], max_tokens=50, temperature=0.2)
    assert seen.bodies[0]["max_completion_tokens"] == 50
    assert "max_tokens" not in seen.bodies[0] and "temperature" not in seen.bodies[0]
    assert seen.bodies[1]["temperature"] == 0.2


async def test_a_provider_without_strict_schemas_is_asked_for_json_in_words() -> None:
    seen = Seen('Here it is:\n```json\n{"a": 1}\n```')
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    done = await seen.ai("anthropic").complete(
        "claude-haiku-4-5", [{"role": "user", "content": "hi"}], json_schema=("x", schema)
    )
    body = seen.bodies[0]
    assert "response_format" not in body
    assert body["messages"][-1]["role"] == "system"
    assert json.dumps(schema) in body["messages"][-1]["content"]
    assert json.loads(done.text) == {"a": 1}


async def test_a_provider_that_cannot_paint_says_so_before_asking() -> None:
    seen = Seen()
    with pytest.raises(AiError) as e:
        await seen.ai("openai").complete("gpt-5.4", [], image_aspect="16:9")
    assert e.value.kind == Kind.UNSUPPORTED
    assert e.value.sentence.startswith("OpenAI cannot paint pictures.")
    assert seen.bodies == []


def test_an_empty_account_answered_with_429_is_out_of_credit_not_busy() -> None:
    quota = json.dumps(
        {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}
    )
    assert AiError.from_status(429, quota).kind == Kind.QUOTA
    busy = json.dumps({"error": {"message": "Rate limit reached for requests per min"}})
    assert AiError.from_status(429, busy).kind == Kind.RATE_LIMITED


# ── the router ────────────────────────────────────────────────────────────────


async def test_the_router_sends_each_model_to_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conns = [conn("openrouter"), conn("openai")]

    async def every() -> list[Connection]:
        return conns

    monkeypatch.setattr(connections, "every", every)
    seen = {c.id: Seen(c.id) for c in conns}
    router = Router()

    def client(c: Connection) -> Ai:
        return seen[c.id].ai(c.kind)

    monkeypatch.setattr(router, "client", client)
    assert (await router.complete("anthropic/claude-haiku-4.5", [])).text == "openrouter"
    assert (await router.complete("openai:gpt-5.4", [])).text == "openai"
    assert seen["openai"].bodies[0]["model"] == "gpt-5.4"


async def test_with_nothing_connected_the_router_says_where_to_connect() -> None:
    with pytest.raises(AiError) as e:
        await Router().complete("anthropic/claude-haiku-4.5", [])
    assert e.value.kind == Kind.UNCONFIGURED
    assert "Settings › AI providers" in e.value.sentence
    assert not await Router().ready()


# ── testing a key ─────────────────────────────────────────────────────────────


def models(*ids: str) -> httpx2.Response:
    return httpx2.Response(200, json={"data": [{"id": i} for i in ids]})


async def test_openrouter_reports_whats_left_on_the_key_without_spending() -> None:
    asked: list[str] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        asked.append(r.url.path)
        if r.url.path.endswith("/key"):
            return httpx2.Response(200, json={"data": {"limit_remaining": 4.126}})
        return models("google/gemini-2.5-flash-lite")

    found = await check(conn("openrouter"), http=http(handler))
    assert found.ok and found.balance == "$4.13 left on this key"
    assert found.sentence == "Connected to OpenRouter: $4.13 left on this key."
    assert not any(p.endswith("/chat/completions") for p in asked)


async def test_an_openrouter_key_with_nothing_left_has_no_credit() -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": {"limit_remaining": 0}})

    found = await check(conn("openrouter"), http=http(handler))
    assert found.status == "no_credit"
    assert "has no credit left" in found.sentence


async def test_a_refused_key_is_a_bad_key() -> None:
    found = await check(conn("openai"), http=http(lambda _: httpx2.Response(401, json={})))
    assert found.status == "bad_key"
    assert found.sentence.startswith("OpenAI refused this key.")
    assert "platform.openai.com/api-keys" in found.sentence


async def test_geminis_invalid_key_is_a_bad_key() -> None:
    found = await check(
        conn("gemini"),
        http=http(lambda _: httpx2.Response(400, json={"error": {"status": "API_KEY_INVALID"}})),
    )
    assert found.status == "bad_key"


async def test_deepseek_reports_its_balance() -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        if r.url.path.endswith("/user/balance"):
            return httpx2.Response(
                200,
                json={
                    "is_available": True,
                    "balance_infos": [{"currency": "USD", "total_balance": "12.5"}],
                },
            )
        return models("deepseek-chat")

    found = await check(conn("deepseek"), http=http(handler))
    assert found.ok and found.balance == "$12.50 left"


async def test_without_a_balance_api_one_tiny_request_shows_an_empty_account() -> None:
    sent: list[dict[str, Any]] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        if r.url.path.endswith("/models"):
            return models("gpt-5.4-mini")
        sent.append(json.loads(r.content))
        return httpx2.Response(
            429, json={"error": {"message": "quota", "code": "insufficient_quota"}}
        )

    found = await check(conn("openai"), http=http(handler))
    assert found.status == "no_credit"
    assert sent[0]["model"] == "gpt-5.4-mini"
    assert sent[0]["max_completion_tokens"] == 16


async def test_a_working_key_without_a_balance_api_says_none_is_reported() -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        return models("gpt-5.4-mini") if r.url.path.endswith("/models") else reply()

    found = await check(conn("openai"), http=http(handler))
    assert found.ok and found.models == 1
    assert found.sentence == "Connected to OpenAI. OpenAI does not report a balance."


async def test_a_server_that_does_not_answer_is_unreachable() -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    found = await check(conn("ollama", key="", url="http://localhost:11434/v1"), http=http(handler))
    assert found.status == "unreachable"
    assert "did not answer at http://localhost:11434/v1" in found.sentence


async def test_a_server_that_is_not_openai_compatible_says_so() -> None:
    found = await check(
        conn("custom", key="", url="http://box.lan:8080"),
        http=http(lambda _: httpx2.Response(404, text="Not Found")),
    )
    assert found.status == "not_compatible"
    assert "usually ends in /v1" in found.sentence


async def test_ollama_with_no_models_says_to_pull_one_and_is_never_probed() -> None:
    found = await check(
        conn("ollama", key="", url="http://localhost:11434/v1"),
        http=http(lambda _: models()),
    )
    assert found.status == "no_models"
    assert "ollama pull" in found.sentence


async def test_a_missing_key_is_asked_for_before_anything_is_sent() -> None:
    found = await check(conn("anthropic", key=""))
    assert found.status == "bad_key"
    assert found.sentence == "Enter your Anthropic API key."


# ── the setup routes ──────────────────────────────────────────────────────────


def passing(balance: str = "") -> Callable[..., Any]:
    async def fake(c: Connection, **_: Any) -> Check:
        return Check("ok", f"Connected to {c.label}.", balance, 3, "2026-10-08T00:00:00+00:00")

    return fake


async def test_with_nothing_connected_the_studio_is_not_ready(client: AsyncClient) -> None:
    r = await client.get("/api/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False and body["can_setup"] is True
    assert body["message"].startswith("Connect an AI provider to start.")


async def test_a_working_key_is_kept_encrypted_and_the_studio_is_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opennotebook.api import providers

    monkeypatch.setattr(providers, "check", passing("$5.00 left"))
    r = await client.post("/api/ai/providers", json={"kind": "openai", "key": "sk-abcdefgh1234"})
    assert r.status_code == 201, r.text
    added = r.json()
    assert added["id"] == "openai" and added["key_hint"] == "…1234"
    assert added["primary"] is True and added["check"]["balance"] == "$5.00 left"
    async with engine().begin() as c:
        stored = (await c.execute(text("SELECT key_encrypted FROM ai_providers"))).scalar_one()
    assert "sk-abcdefgh1234" not in stored
    assert secrets.unseal(stored) == "sk-abcdefgh1234"
    assert (await client.get("/api/setup")).json()["ready"] is True


async def test_a_failing_key_is_refused_with_its_reason(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opennotebook.api import providers

    async def failing(c: Connection, **_: Any) -> Check:
        return Check("bad_key", "OpenAI refused this key.", checked_at="t")

    monkeypatch.setattr(providers, "check", failing)
    r = await client.post("/api/ai/providers", json={"kind": "openai", "key": "sk-nope"})
    assert r.status_code == 422
    assert r.json()["detail"] == "OpenAI refused this key."
    assert (await client.get("/api/setup")).json()["ready"] is False


async def test_a_key_can_be_tested_without_being_kept(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opennotebook.api import providers

    monkeypatch.setattr(providers, "check", passing())
    r = await client.post("/api/ai/providers/check", json={"kind": "gemini", "key": "g-key"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await client.get("/api/setup")).json()["providers"] == []


async def test_an_unknown_provider_or_address_is_refused(client: AsyncClient) -> None:
    r = await client.post("/api/ai/providers", json={"kind": "skynet", "key": "k"})
    assert r.status_code == 422 and "Pick one from the list" in r.json()["detail"]
    r = await client.post("/api/ai/providers", json={"kind": "custom", "base_url": "box:80"})
    assert r.status_code == 422 and "http://" in r.json()["detail"]


async def test_a_connected_provider_can_be_removed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opennotebook.api import providers

    monkeypatch.setattr(providers, "check", passing())
    await client.post("/api/ai/providers", json={"kind": "mistral", "key": "m-key"})
    assert (await client.delete("/api/ai/providers/mistral")).status_code == 204
    assert (await client.get("/api/setup")).json()["ready"] is False
    assert (await client.delete("/api/ai/providers/mistral")).status_code == 404


async def test_a_provider_set_in_the_environment_is_changed_only_there(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env-9999")
    connections.forget()
    body = (await client.get("/api/setup")).json()
    assert body["ready"] is True
    [p] = body["providers"]
    assert (p["id"], p["source"], p["key_hint"]) == ("openrouter", "env", "…9999")
    r = await client.delete("/api/ai/providers/openrouter")
    assert r.status_code == 409
    assert "set in the server's environment" in r.json()["detail"]


async def test_only_whoever_runs_the_studio_connects_providers(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config(), "auth", "keys")
    them = await other_person(client, "guest@example.com")
    body = (await client.get("/api/setup", headers=them)).json()
    assert body["can_setup"] is False
    assert body["message"].startswith("Only whoever runs this studio")
    r = await client.post("/api/ai/providers", json={"kind": "openai", "key": "k"}, headers=them)
    assert r.status_code == 403


async def test_the_providers_list_offers_the_presets_and_each_roles_model(
    client: AsyncClient,
) -> None:
    body = (await client.get("/api/ai/providers")).json()
    kinds = [p["kind"] for p in body["presets"]]
    assert kinds[0] == "openrouter" and "anthropic" in kinds and "ollama" in kinds
    roles = {r["role"]: r for r in body["roles"]}
    # Nothing connected: no role has a model yet.
    assert roles["chat"]["model"] == ""
    assert roles["long"]["keys"] == [st.MINDMAP_MODEL_KEY, st.NOTES_MODEL_KEY]


# ── settings follow the connections ───────────────────────────────────────────


def test_a_models_default_follows_the_connected_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    connections.forget()
    connections.remember_offered({"openai": {"gpt-5-mini", "gpt-5.4"}})
    d = st.find(st.SCRIPT_MODEL_KEY)
    assert d is not None
    assert st.default_of(d) == "gpt-5-mini"
    assert st.effective(st.SLIDE_MODEL_KEY, {}, {}, {}) == "gpt-5.4"
    # A role OpenAI has no model for keeps the catalogue's.
    image = st.find(st.VIDEO_IMAGE_MODEL_KEY)
    assert image is not None and st.default_of(image) == image.default


def test_a_second_provider_fills_the_roles_the_first_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    connections.forget()
    assert connections.default_for("text", {}) == "claude-haiku-5-5"
    assert connections.default_for("audio", {}) == "gemini:gemini-2.5-flash"
