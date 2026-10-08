"""The AI providers this studio is connected to.

A connection is one provider account: a preset (`ai/providers.py`), an
address, a key. They come from two places:

1. The environment, set by whoever runs the studio: a provider's own key
   variable (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …), or the studio's
   `OPENNOTEBOOK_AI_KEY` with `OPENNOTEBOOK_AI_BASE_URL`, which connects
   OpenRouter unless the address is another provider's. These are shown in
   the web app and can be tested there, but only changed where they are set.
2. The web app's setup tour and Settings › AI providers, stored in
   `ai_providers` with the key encrypted (`ai/secrets.py`).

The first connection is the primary one: a model named without a connection
(`anthropic/claude-haiku-4.5`) is asked of it, so every value stored before
connections existed keeps working. Any other is named `connection:model`
(`openai:gpt-5.4`). The environment's come first; `OPENNOTEBOOK_AI_PRIMARY`
names another.

The list is read at most every `CACHE_SECONDS` and dropped at once when this
process changes it; another process (the worker) sees a change within
`CACHE_SECONDS`.
"""

import asyncio
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from opennotebook.ai import secrets
from opennotebook.ai.providers import BY_KIND, Preset, Role, kind_of_url, preset
from opennotebook.db.models import AiProvider
from opennotebook.db.session import sessionmaker

PRIMARY_ENV = "OPENNOTEBOOK_AI_PRIMARY"
CACHE_SECONDS = 5.0

type Source = Literal["env", "app"]


@dataclass(frozen=True)
class Connection:
    id: str
    kind: str
    label: str
    base_url: str
    key: str
    source: Source
    # What testing it last said (`ai/check.py`), for a stored one.
    last_check: Mapping[str, Any] | None = None

    @property
    def preset(self) -> Preset:
        return preset(self.kind)

    @property
    def usable(self) -> bool:
        """Whether calls can go out: an address, and a key when it needs one."""
        return bool(self.base_url) and (bool(self.key) or not self.preset.needs_key)

    def __repr__(self) -> str:
        # Never the key, in a log or a traceback.
        return f"Connection({self.id!r}, {self.kind!r}, {self.base_url!r}, {self.source})"


# ── from the environment ──────────────────────────────────────────────────────


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for n in names:
        if v := env.get(n, "").strip():
            return v
    return ""


def from_env(env: Mapping[str, str] | None = None) -> list[Connection]:
    """The connections the environment sets, the studio's own first."""
    e = os.environ if env is None else env
    out: list[Connection] = []
    # The studio's own pair, under the names the settings read it by.
    key = _first(e, ("OPENNOTEBOOK_AI_API_KEY", "OPENNOTEBOOK_AI_KEY"))
    url = e.get("OPENNOTEBOOK_AI_BASE_URL", "").strip() or BY_KIND["openrouter"].base_url
    if key:
        kind = kind_of_url(url)
        p = preset(kind)
        out.append(Connection(kind, kind, p.label, url.rstrip("/"), key, "env"))
    taken = {c.id for c in out}
    for p in BY_KIND.values():
        if p.kind in taken or p.kind == "custom":
            continue
        key = _first(e, p.key_env)
        url = _first(e, p.url_env)
        if key or (url and not p.needs_key):
            base = (url or p.base_url).rstrip("/")
            out.append(Connection(p.kind, p.kind, p.label, base, key, "env"))
    return out


# ── stored ────────────────────────────────────────────────────────────────────


async def _stored() -> list[Connection]:
    async with sessionmaker()() as s:
        rows = (await s.execute(select(AiProvider).order_by(AiProvider.created_at))).scalars()
        return [
            Connection(
                r.id,
                r.kind,
                r.label or preset(r.kind).label,
                r.base_url,
                secrets.unseal(r.key_encrypted),
                "app",
                r.last_check,
            )
            for r in rows
        ]


def _ordered(env: list[Connection], app: list[Connection]) -> list[Connection]:
    seen = {c.id for c in env}
    out = env + [c for c in app if c.id not in seen]
    want = os.environ.get(PRIMARY_ENV, "").strip()
    out.sort(key=lambda c: c.id != want)
    return out


_kept: list[tuple[float, list[Connection]]] = []
_reading: asyncio.Lock | None = None


async def every() -> list[Connection]:
    """Every connection, the primary first: what is kept when it is fresh,
    else read again."""
    global _reading
    if _kept and time.monotonic() - _kept[0][0] < CACHE_SECONDS:
        return _kept[0][1]
    if _reading is None:
        _reading = asyncio.Lock()
    async with _reading:
        if _kept and time.monotonic() - _kept[0][0] < CACHE_SECONDS:
            return _kept[0][1]
        conns = _ordered(from_env(), await _stored())
        _kept[:] = [(time.monotonic(), conns)]
        return conns


def snapshot() -> list[Connection]:
    """The connections as last read, for code that cannot wait. Before the
    first read, the environment's alone."""
    return _kept[0][1] if _kept else _ordered(from_env(), [])


def forget() -> None:
    _kept.clear()


async def usable() -> list[Connection]:
    return [c for c in await every() if c.usable]


async def find(cid: str) -> Connection | None:
    return next((c for c in await every() if c.id == cid), None)


# ── model references ──────────────────────────────────────────────────────────


def split(ref: str, conns: list[Connection]) -> tuple[Connection | None, str]:
    """The connection a model reference names and the model's own id.
    `openai:gpt-5.4` is OpenAI's `gpt-5.4`; anything whose part before the
    first colon is not a connection (`anthropic/claude-haiku-4.5`, Ollama's
    `llama3.2:3b`) is the primary connection's, whole."""
    head, colon, rest = ref.partition(":")
    if colon and rest:
        for c in conns:
            if c.id == head:
                return c, rest
    primary = next((c for c in conns if c.usable), None)
    return primary, ref


def ref(c: Connection, model: str, conns: list[Connection]) -> str:
    """How the settings name `model` of `c`: bare on the primary connection,
    as before connections existed, and `id:model` on any other."""
    primary = next((x for x in conns if x.usable), None)
    return model if primary is not None and primary.id == c.id else f"{c.id}:{model}"


# ── choosing a model for a role ───────────────────────────────────────────────


def suggest(c: Connection, role: Role, offered: set[str] | None) -> str | None:
    """`c`'s model for `role`: its preset's first suggestion that `c` offers,
    or, when its list is not known, its first suggestion."""
    for m in c.preset.suggest.get(role, ()):
        if not offered or m in offered:
            return m
    return None


_offered: dict[str, set[str]] = {}


def remember_offered(offered: Mapping[str, set[str]]) -> None:
    """Keep each connection's model list, as its catalogue last read it."""
    _offered.clear()
    _offered.update(offered)


def default_for(role: Role, offered: Mapping[str, set[str]] | None = None) -> str | None:
    """The model a role uses when nobody chose one: the first usable
    connection, primary first, that suggests one. `offered` is each
    connection's model list where known; by default, the one remembered."""
    conns = snapshot()
    lists = _offered if offered is None else offered
    for c in conns:
        if not c.usable:
            continue
        if m := suggest(c, role, lists.get(c.id)):
            return ref(c, m, conns)
    return None


# ── changing them ─────────────────────────────────────────────────────────────

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def new_id(kind: str, taken: set[str]) -> str:
    """An id for a new connection of `kind`: the kind itself, then `kind-2`,
    `kind-3`, …"""
    if kind not in taken:
        return kind
    n = 2
    while f"{kind}-{n}" in taken:
        n += 1
    return f"{kind}-{n}"


def valid_id(cid: str) -> bool:
    return bool(_SLUG.fullmatch(cid))


async def save(c: Connection, last_check: Mapping[str, Any] | None) -> None:
    values = {
        "id": c.id,
        "kind": c.kind,
        "label": c.label,
        "base_url": c.base_url,
        "key_encrypted": secrets.seal(c.key),
        "last_check": dict(last_check) if last_check is not None else None,
    }
    async with sessionmaker()() as s, s.begin():
        stmt = insert(AiProvider).values(**values)
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[AiProvider.id],
                set_={k: v for k, v in values.items() if k != "id"} | {"updated_at": func.now()},
            )
        )
    forget()


async def remember_check(cid: str, last_check: Mapping[str, Any]) -> None:
    """Keep what testing a stored connection said."""
    async with sessionmaker()() as s, s.begin():
        await s.execute(
            update(AiProvider).where(AiProvider.id == cid).values(last_check=dict(last_check))
        )
    forget()


async def remove(cid: str) -> bool:
    async with sessionmaker()() as s, s.begin():
        gone = await s.execute(delete(AiProvider).where(AiProvider.id == cid))
    forget()
    return bool(getattr(gone, "rowcount", 0))
