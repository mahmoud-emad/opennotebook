"""Testing a connection before it is trusted: does the provider answer, does
it accept the key, and is there credit to spend.

1. A provider with an endpoint for it says so for free: OpenRouter's
   `GET /key` (its limit and what is left of it), DeepSeek's
   `GET /user/balance`.
2. Every provider's `GET /models` proves the key and the address, and lists
   what can be chosen.
3. Where no balance can be read, one tiny request is made, with a cheap
   model and a one-word answer: a fraction of a cent, recorded in the spend
   ledger like any call. Most providers have no balance API, and an empty
   account only shows when something is asked of it. A local server is not
   asked: it has no account.

The result is one of a few states, each with the sentence a person sees.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx2

from opennotebook.ai.client import Ai
from opennotebook.ai.connections import Connection, suggest
from opennotebook.ai.errors import AiError, Kind
from opennotebook.ai.prices import auth_headers, model_ids

type Status = Literal["ok", "bad_key", "no_credit", "unreachable", "not_compatible", "no_models"]

TIMEOUT_S = 20.0
# A probe asks for a word: enough room for a model that thinks first.
PROBE_TOKENS = 16


@dataclass
class Check:
    status: Status
    sentence: str
    # What is left to spend, in words (`$4.12 left`); empty when the provider
    # does not say.
    balance: str = ""
    models: int = 0
    checked_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _money(v: float, currency: str = "USD") -> str:
    sign = {"USD": "$", "CNY": "¥", "EUR": "€"}.get(currency.upper(), "")
    amount = f"{v:,.2f}"
    return f"{sign}{amount}" if sign else f"{amount} {currency}"


def _said(c: Connection, status: Status, detail: str = "") -> str:
    p = c.preset
    where = c.base_url
    match status:
        case "ok":
            return f"Connected to {c.label}."
        case "bad_key":
            page = f" You can make a new one at {p.key_page}." if p.key_page else ""
            return f"{c.label} refused this key. Check that you copied all of it.{page}"
        case "no_credit":
            return (
                f"The key works, but the {c.label} account has no credit left. Add credit to "
                "it, then test again."
            )
        case "unreachable":
            return (
                f"{c.label} did not answer at {where}. Check the address and this machine's "
                "connection, then test again."
            )
        case "not_compatible":
            return (
                f"The server at {where} does not answer like an OpenAI-compatible API. Check "
                "the address: it usually ends in /v1."
            )
        case "no_models":
            if c.kind == "ollama":
                return (
                    "Ollama answered but has no models yet. Pull one first (for example, "
                    "`ollama pull llama3.2`), then test again."
                )
            return f"{c.label} answered but offers no models. {detail}".strip()


def _fail(c: Connection, status: Status, detail: str = "") -> Check:
    return Check(status, _said(c, status, detail), checked_at=_now())


async def check(
    c: Connection, *, probe: bool = True, http: httpx2.AsyncClient | None = None
) -> Check:
    """What testing `c` says. Never raises for the provider's sake: every
    failure is a state with its sentence."""
    if not c.base_url.strip():
        return Check(
            "not_compatible",
            "Enter the server's address, such as http://localhost:1234/v1.",
            checked_at=_now(),
        )
    if c.preset.needs_key and not c.key.strip():
        return Check("bad_key", f"Enter your {c.label} API key.", checked_at=_now())
    own = http is None
    client = http or httpx2.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        return await _check(c, client, probe)
    finally:
        if own:
            await client.aclose()


async def _check(c: Connection, http: httpx2.AsyncClient, probe: bool) -> Check:
    base = c.base_url.rstrip("/")
    headers = auth_headers(c.kind, c.key)
    balance = ""
    # Whether the balance is known well enough to need no probe.
    known = False

    try:
        if c.preset.key_check == "openrouter":
            r = await http.get(f"{base}/key", headers=headers)
            if r.status_code in (401, 403):
                return _fail(c, "bad_key")
            if r.is_success:
                data: Any = (r.json() or {}).get("data") or {}
                left = data.get("limit_remaining")
                if isinstance(left, int | float):
                    if left <= 0:
                        return _fail(c, "no_credit")
                    balance, known = f"{_money(float(left))} left on this key", True
        elif c.preset.key_check == "deepseek":
            r = await http.get(f"{base}/user/balance", headers=headers)
            if r.status_code in (401, 403):
                return _fail(c, "bad_key")
            if r.is_success:
                body: Any = r.json() or {}
                if body.get("is_available") is False:
                    return _fail(c, "no_credit")
                for info in body.get("balance_infos") or []:
                    try:
                        balance = f"{_money(float(info['total_balance']), info['currency'])} left"
                        known = True
                        break
                    except KeyError, TypeError, ValueError:
                        continue

        r = await http.get(f"{base}/models", headers=headers)
    except httpx2.TimeoutException, httpx2.ConnectError:
        return _fail(c, "unreachable")
    except httpx2.HTTPError:
        return _fail(c, "unreachable")
    except ValueError:
        return _fail(c, "not_compatible")

    if r.status_code in (401, 403) or (r.status_code == 400 and "API_KEY_INVALID" in r.text):
        return _fail(c, "bad_key")
    if r.status_code == 402:
        return _fail(c, "no_credit")
    if not r.is_success:
        if r.status_code >= 500:
            return _fail(c, "unreachable")
        return _fail(c, "not_compatible")
    try:
        ids = model_ids(r.json())
    except ValueError:
        return _fail(c, "not_compatible")
    if not ids:
        return _fail(c, "no_models")

    if probe and not known and c.preset.needs_key:
        model = suggest(c, "chat", ids) or suggest(c, "text", ids)
        if model is not None:
            failed = await _probe(c, http, model)
            if failed is not None:
                return failed

    said = _said(c, "ok")
    if balance:
        said = f"{said[:-1]}: {balance}."
    elif c.preset.needs_key:
        said = f"{said} {c.label} does not report a balance."
    return Check("ok", said, balance, len(ids), _now())


async def _probe(c: Connection, http: httpx2.AsyncClient, model: str) -> Check | None:
    """One tiny request; the failing state it shows, or None when it went
    through."""
    ai = Ai(c.base_url, c.key, http=http, retries=0, timeout=TIMEOUT_S, preset=c.preset)
    try:
        await ai.complete(model, [{"role": "user", "content": "Say OK."}], max_tokens=PROBE_TOKENS)
    except AiError as e:
        match e.kind:
            case Kind.QUOTA:
                return _fail(c, "no_credit")
            case Kind.AUTH:
                return _fail(c, "bad_key")
            case Kind.UNAVAILABLE:
                return _fail(c, "unreachable")
            case _:
                # A busy provider, a model it has dropped, a request it
                # did not like: the key and the account answered, which is
                # what is being tested.
                return None
    return None
