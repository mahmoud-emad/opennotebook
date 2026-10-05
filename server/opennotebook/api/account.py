"""The person asking, their API keys, and whether the studio is up and ready."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update

from opennotebook.api import paging
from opennotebook.api.deps import Db, Me
from opennotebook.auth import key_hash, new_key
from opennotebook.db.models import ApiKey
from opennotebook.db.session import sessionmaker
from opennotebook.domain import sessions
from opennotebook.errors import not_found
from opennotebook.jobs.events import hub

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["account"])


class Health(BaseModel):
    ok: bool


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str


class KeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str = Field(description="The key's first characters, to tell keys apart")
    created_at: datetime
    last_used_at: datetime | None


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MadeKey(KeyOut):
    key: str = Field(description="The key itself. Shown this once; only its hash is kept")


@router.get("/health")
async def health(s: Db) -> Health:
    """Up, and the database answers."""
    await s.execute(text("SELECT 1"))
    return Health(ok=True)


class Check(BaseModel):
    ok: bool
    detail: str = Field(description="What was found, in a sentence")


class Readiness(BaseModel):
    ready: bool = Field(description="Everything below is ok: the studio can take work")
    database: Check
    listener: Check = Field(description="The connection that carries progress to open pages")
    worker: Check = Field(description="The process that runs builds and research")


# How long readiness waits on the database before saying it is not there.
READY_TIMEOUT_S = 5.0


async def _database() -> tuple[Check, Check]:
    """The database and the worker's heartbeat in it."""
    try:
        async with asyncio.timeout(READY_TIMEOUT_S), sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
            alive = await sessions.worker_alive(s)
    except Exception as e:
        log.warning("readiness: the database did not answer: %s", e)
        down = Check(
            ok=False,
            detail="The database is not answering. Check that Postgres is running and that "
            "DATABASE_URL points at it.",
        )
        unknown = Check(ok=False, detail="Unknown while the database is not answering.")
        return down, unknown
    worker = (
        Check(ok=True, detail="A worker reported in within the last 30 seconds.")
        if alive
        else Check(
            ok=False,
            detail="No worker has reported in, so builds and research wait in the queue. "
            "Start one with: opennotebook worker.",
        )
    )
    return Check(ok=True, detail="The database answers."), worker


def _listener() -> Check:
    if hub.connected:
        return Check(ok=True, detail="Connected.")
    if hub.listening and hub.failure:
        return Check(
            ok=False,
            detail="The progress listener lost its database connection and is reconnecting, "
            "so open pages do not see progress until it is back.",
        )
    return Check(
        ok=True,
        detail="Not connected, because no page is following an output; it connects when one is.",
    )


@router.get("/ready", responses={503: {"model": Readiness, "description": "Not ready"}})
async def ready(response: Response) -> Readiness:
    """Whether the studio can take work: the database answers, the progress
    listener is connected when it is needed, and a worker is running. 503
    with the same body when any is not. `/api/health` only says the api is
    up."""
    database, worker = await _database()
    listener = _listener()
    out = Readiness(
        ready=database.ok and listener.ok and worker.ok,
        database=database,
        listener=listener,
        worker=worker,
    )
    if not out.ready:
        response.status_code = 503
    return out


@router.get("/me")
async def me(me: Me) -> MeOut:
    return MeOut.model_validate(me, from_attributes=True)


# How many keys a page of the list holds unless asked for fewer, and at most.
KEYS_DEFAULT = 100
KEYS_MAX = 500


@router.get("/keys")
async def list_keys(
    s: Db,
    me: Me,
    response: Response,
    limit: Annotated[int, paging.limit(KEYS_DEFAULT, KEYS_MAX, "keys")] = KEYS_DEFAULT,
    offset: paging.Offset = 0,
) -> list[KeyOut]:
    """Your API keys that still work, newest first, a page at a time:
    `X-Next-Offset` says where the next page starts when there is one."""
    rows = await s.scalars(
        select(ApiKey)
        .where(ApiKey.owner_id == me.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    return [
        KeyOut.model_validate(k, from_attributes=True)
        for k in paging.cut(list(rows), limit, offset, response)
    ]


@router.post("/keys", status_code=201)
async def make_key(body: NewKey, s: Db, me: Me) -> MadeKey:
    """Make an API key for an agent or a script to act as you."""
    key = new_key()
    k = ApiKey(owner_id=me.id, name=body.name.strip(), prefix=key[:12], hash=key_hash(key))
    s.add(k)
    await s.flush()
    await s.refresh(k)
    return MadeKey(**KeyOut.model_validate(k, from_attributes=True).model_dump(), key=key)


@router.delete("/keys/{kid}", status_code=204)
async def revoke_key(kid: uuid.UUID, s: Db, me: Me) -> None:
    """Revoke a key. Anything using it stops working at once."""
    done = await s.execute(
        update(ApiKey)
        .where(ApiKey.id == kid, ApiKey.owner_id == me.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
        .returning(ApiKey.id)
    )
    if done.first() is None:
        raise not_found("That key")
