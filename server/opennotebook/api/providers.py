"""The AI providers the studio is connected to, and whether it is set up.

The web app asks `GET /api/setup` before it draws anything: until a provider
is connected it shows the setup tour, which connects one through these
routes. Every key is tested before it is kept (`ai/check.py`), and a key that
fails is refused with the sentence that says why.

Only whoever runs the studio connects providers; the keys serve everyone.
Providers set in the server's environment are listed and can be tested, but
are changed where they are set.
"""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from opennotebook.ai import connections, ledger
from opennotebook.ai.check import Check, check
from opennotebook.ai.client import router as ai_router
from opennotebook.ai.connections import Connection
from opennotebook.ai.providers import BY_KIND, PRESETS, ROLE_LABELS, ROLES, Role, preset
from opennotebook.api.deps import Me
from opennotebook.db.models import User
from opennotebook.domain import settings
from opennotebook.domain.sessions_estimate import model_name
from opennotebook.errors import Problem

router = APIRouter(prefix="/api", tags=["setup"])

NOT_OPERATOR = (
    "Only whoever runs this studio can connect AI providers. Ask them to connect one, then "
    "reload this page."
)
MAX_KEY_CHARS = 500
MAX_URL_CHARS = 500


# ── shapes ────────────────────────────────────────────────────────────────────


class CheckOut(BaseModel):
    status: Literal["ok", "bad_key", "no_credit", "unreachable", "not_compatible", "no_models"]
    ok: bool
    sentence: str = Field(description="What the test found, as the page says it")
    balance: str = Field(description="What is left to spend, in words; empty when not reported")
    models: int = Field(description="How many models the provider offers")
    checked_at: str

    @classmethod
    def of(cls, c: Check) -> CheckOut:
        return cls(
            status=c.status,
            ok=c.ok,
            sentence=c.sentence,
            balance=c.balance,
            models=c.models,
            checked_at=c.checked_at,
        )


class PresetOut(BaseModel):
    kind: str
    label: str
    blurb: str
    base_url: str = Field(description="Its usual address; empty for a custom server")
    needs_key: bool
    key_page: str = Field(description="Where to make a key, without the scheme")
    key_env: str = Field(description="The environment variable that sets its key instead")
    roles: list[str] = Field(description="The roles it suggests a model for")


class ProviderOut(BaseModel):
    id: str
    kind: str
    label: str
    base_url: str
    source: Literal["env", "app"] = Field(
        description="env: set in the server's environment, changed only there"
    )
    key_hint: str = Field(description="The key's last characters, to tell keys apart")
    primary: bool = Field(description="Models named without a provider are this one's")
    check: CheckOut | None = Field(description="What testing it last said; none for env ones")


class RoleOut(BaseModel):
    role: str
    label: str
    keys: list[str] = Field(description="The model settings this role fills")
    model: str = Field(description="The model in force; empty when no provider can do it")
    model_name: str
    chosen: bool = Field(description="Set by someone, rather than suggested")


class SetupOut(BaseModel):
    ready: bool = Field(description="A provider is connected, so the studio can be used")
    can_setup: bool = Field(description="This person may connect providers")
    providers: list[ProviderOut]
    message: str = Field(description="What the page says when it is not ready")


class ProvidersOut(BaseModel):
    presets: list[PresetOut]
    providers: list[ProviderOut]
    roles: list[RoleOut]


class ProviderIn(BaseModel):
    kind: str = Field(max_length=40)
    base_url: str = Field(default="", max_length=MAX_URL_CHARS)
    key: str = Field(default="", max_length=MAX_KEY_CHARS)
    label: str = Field(default="", max_length=80)


class ModelsOut(BaseModel):
    models: list[str]


# ── helpers ───────────────────────────────────────────────────────────────────


def _hint(key: str) -> str:
    k = key.strip()
    return f"…{k[-4:]}" if len(k) >= 12 else ("set" if k else "")


def _provider(c: Connection, primary: str | None) -> ProviderOut:
    last = c.last_check
    shown = None
    if last:
        try:
            shown = CheckOut.of(Check(**dict(last)))
        except TypeError, ValueError:
            shown = None
    return ProviderOut(
        id=c.id,
        kind=c.kind,
        label=c.label,
        base_url=c.base_url,
        source=c.source,
        key_hint=_hint(c.key),
        primary=c.id == primary,
        check=shown,
    )


async def _providers() -> list[ProviderOut]:
    conns = await connections.every()
    primary = next((c.id for c in conns if c.usable), None)
    return [_provider(c, primary) for c in conns]


def _operator(me: User) -> None:
    if not settings.is_operator(me):
        raise Problem(403, NOT_OPERATOR)


def _connection(body: ProviderIn, cid: str, existing: Connection | None) -> Connection:
    if body.kind not in BY_KIND:
        raise Problem(422, "That is not a provider the studio knows. Pick one from the list.")
    p = preset(body.kind)
    url = body.base_url.strip() or p.base_url
    if url and not url.startswith(("http://", "https://")):
        raise Problem(422, "The address must start with http:// or https://.")
    key = body.key.strip()
    if not key and existing is not None and existing.kind == body.kind:
        # Kept when only the address or the name changes.
        key = existing.key
    return Connection(cid, p.kind, body.label.strip() or p.label, url.rstrip("/"), key, "app", None)


async def _tested(c: Connection, me: User) -> Check:
    # A provider with no balance to read is asked one tiny question, which
    # is paid for like any call.
    async with ledger.spending(me.id, "setup"):
        return await check(c)


async def _roles() -> list[RoleOut]:
    await settings.learn_models()
    keys: dict[Role, list[str]] = {}
    for key, role in settings.ROLE_OF.items():
        keys.setdefault(role, []).append(key)
    out: list[RoleOut] = []
    instance = await _instance_values()
    for role in ROLES:
        if role not in keys:
            continue
        first = keys[role][0]
        chosen = settings.from_env(first) or instance.get(first, "")
        d = settings.find(first)
        model = chosen or (settings.default_of(d) if d else "")
        # A default that no connected provider suggests is the catalogue's
        # own: there is no model for this role.
        if not chosen and connections.default_for(role) is None:
            model = ""
        out.append(
            RoleOut(
                role=role,
                label=ROLE_LABELS[role],
                keys=keys[role],
                model=model,
                model_name=model_name(model) if model else "",
                chosen=bool(chosen),
            )
        )
    return out


async def _instance_values() -> dict[str, str]:
    from opennotebook.db.session import sessionmaker

    async with sessionmaker()() as s:
        return await settings.instance_rows(s)


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/setup")
async def get_setup(me: Me) -> SetupOut:
    """Whether the studio can be used yet, and whether this person can set
    it up. The web app shows the setup tour until it is ready."""
    ready = bool(await connections.usable())
    can = settings.is_operator(me)
    message = ""
    if not ready:
        message = (
            "Connect an AI provider to start. OpenNotebook reads your sources and makes "
            "everything with the provider you choose."
            if can
            else NOT_OPERATOR
        )
    return SetupOut(ready=ready, can_setup=can, providers=await _providers(), message=message)


@router.get("/ai/providers")
async def list_providers(me: Me) -> ProvidersOut:
    """The providers the studio can connect to, the ones it is connected to,
    and the model each kind of work uses."""
    return ProvidersOut(
        presets=[
            PresetOut(
                kind=p.kind,
                label=p.label,
                blurb=p.blurb,
                base_url=p.base_url,
                needs_key=p.needs_key,
                key_page=p.key_page,
                key_env=(p.key_env or p.url_env or ("",))[0],
                roles=[r for r in ROLES if r in p.suggest],
            )
            for p in PRESETS
        ],
        providers=await _providers(),
        roles=await _roles(),
    )


@router.post("/ai/providers/check")
async def check_provider_key(body: ProviderIn, me: Me) -> CheckOut:
    """Test a provider's key without keeping it: does the provider answer,
    accept the key, and have credit."""
    _operator(me)
    taken = {c.id for c in await connections.every()}
    c = _connection(body, connections.new_id(body.kind, taken), None)
    return CheckOut.of(await _tested(c, me))


@router.post("/ai/providers", status_code=201)
async def add_provider(body: ProviderIn, me: Me) -> ProviderOut:
    """Test a provider's key and, when it works, connect it. A key that does
    not work is refused with the reason."""
    _operator(me)
    conns = await connections.every()
    c = _connection(body, connections.new_id(body.kind, {x.id for x in conns}), None)
    found = await _tested(c, me)
    if not found.ok:
        raise Problem(422, found.sentence)
    await connections.save(c, found.as_dict())
    added = await connections.find(c.id)
    assert added is not None
    return _provider(added, next((x.id for x in await connections.usable()), None))


@router.put("/ai/providers/{cid}")
async def replace_provider(cid: str, body: ProviderIn, me: Me) -> ProviderOut:
    """Change a connected provider's key, address or name. The key is tested
    first; an empty key keeps the one it has."""
    _operator(me)
    existing = await _app_connection(cid)
    c = _connection(body, cid, existing)
    found = await _tested(c, me)
    if not found.ok:
        raise Problem(422, found.sentence)
    await connections.save(c, found.as_dict())
    changed = await connections.find(cid)
    assert changed is not None
    return _provider(changed, next((x.id for x in await connections.usable()), None))


@router.delete("/ai/providers/{cid}", status_code=204)
async def remove_provider(cid: str, me: Me) -> Response:
    """Disconnect a provider connected in the web app; its key is deleted."""
    _operator(me)
    await _app_connection(cid)
    await connections.remove(cid)
    return Response(status_code=204)


@router.post("/ai/providers/{cid}/check")
async def recheck_provider(cid: str, me: Me) -> CheckOut:
    """Test a connected provider again, as it is now."""
    _operator(me)
    c = await connections.find(cid)
    if c is None:
        raise _gone()
    found = await _tested(c, me)
    if c.source == "app":
        await connections.remember_check(cid, found.as_dict())
    return CheckOut.of(found)


@router.get("/ai/providers/{cid}/models")
async def provider_models(cid: str, me: Me) -> ModelsOut:
    """The models a connected provider offers, by the ids the settings take
    (`provider:model`, bare on the primary provider)."""
    conns = await connections.every()
    c = next((x for x in conns if x.id == cid), None)
    if c is None:
        raise _gone()
    if not c.usable:
        return ModelsOut(models=[])
    ids = await ai_router().client(c).catalogue.ids()
    return ModelsOut(models=sorted(connections.ref(c, m, conns) for m in ids))


def _gone() -> Problem:
    return Problem(
        404, "That provider is no longer connected. Reload the page to see the ones that are."
    )


async def _app_connection(cid: str) -> Connection:
    c = await connections.find(cid)
    if c is None:
        raise _gone()
    if c.source == "env":
        raise Problem(
            409,
            f"{c.label} is set in the server's environment, so it can only be changed or "
            "removed there.",
        )
    return c
