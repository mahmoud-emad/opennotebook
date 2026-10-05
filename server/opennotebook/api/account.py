"""The person asking, their API keys, and whether the studio is up."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update

from opennotebook.api.deps import Db, Me
from opennotebook.auth import key_hash, new_key
from opennotebook.db.models import ApiKey
from opennotebook.errors import not_found

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


@router.get("/me")
async def me(me: Me) -> MeOut:
    return MeOut.model_validate(me, from_attributes=True)


@router.get("/keys")
async def list_keys(s: Db, me: Me) -> list[KeyOut]:
    """Your API keys that still work, newest first."""
    rows = await s.scalars(
        select(ApiKey)
        .where(ApiKey.owner_id == me.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    return [KeyOut.model_validate(k, from_attributes=True) for k in rows]


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
