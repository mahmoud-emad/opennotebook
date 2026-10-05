"""Who is asking.

Every request resolves to one `User` through `current_user`, and that is the
only place that decides how. Today there are two ways in:

- an API key, sent as `Authorization: Bearer onk_…`, for agents, scripts and
  any client once sign-in exists;
- in `local` mode (OPENNOTEBOOK_AUTH=local, the default), a request without a
  key is the studio's one owner, made on first use. That is the studio run by
  one person on their own machine, as it is today.

How people sign in (passwords, a provider, single sign-on) is an open
question (docs/open-questions.md). It lands here as a third way in, and
nothing else changes.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.config import settings
from opennotebook.db.models import ApiKey, User
from opennotebook.db.session import db, sessionmaker
from opennotebook.errors import Problem

KEY_PREFIX = "onk_"

# How stale a key's "last used" may get before a request writes it again: a
# key in steady use is written once a minute, not on every request.
LAST_USED_EVERY = timedelta(seconds=60)


def new_key() -> str:
    """A fresh API key: shown to its owner once, stored only as its hash."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    # The key is 256 random bits, so a fast hash is enough; a slow one would
    # only slow every request down.
    return hashlib.sha256(key.encode()).hexdigest()


async def local_owner(s: AsyncSession) -> User:
    email = settings().local_owner_email
    await s.execute(
        text(
            "INSERT INTO users (email, display_name) VALUES (:email, 'Owner') "
            "ON CONFLICT ((lower(email))) DO NOTHING"
        ),
        {"email": email},
    )
    user = await s.scalar(
        select(User).where(text("lower(email) = lower(:email)")), {"email": email}
    )
    assert user is not None
    return user


async def _by_key(s: AsyncSession, key: str) -> User:
    found = (
        await s.execute(
            select(ApiKey, User)
            .join(User, User.id == ApiKey.owner_id)
            .where(ApiKey.hash == key_hash(key), ApiKey.revoked_at.is_(None))
        )
    ).first()
    if found is None:
        raise Problem(401, "That API key is not valid or was revoked. Make a new one in Settings.")
    api_key, user = found
    now = datetime.now(UTC)
    if api_key.last_used_at is None or now - api_key.last_used_at >= LAST_USED_EVERY:
        await _mark_used(api_key.id, now)
    return user


async def _mark_used(key_id: uuid.UUID, now: datetime) -> None:
    """Note when a key was used, in a transaction of its own that ends at
    once: the request's own transaction may last a minute, and a row lock on
    the key held that long would queue every other request that uses it."""
    async with sessionmaker()() as t, t.begin():
        await t.execute(
            update(ApiKey)
            .where(
                ApiKey.id == key_id,
                or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at < now - LAST_USED_EVERY),
            )
            .values(last_used_at=now)
        )


async def current_user(
    request: Request, s: Annotated[AsyncSession, Depends(db, scope="function")]
) -> User:
    header = request.headers.get("authorization", "")
    scheme, _, key = header.partition(" ")
    if scheme.lower() == "bearer" and key.strip():
        user = await _by_key(s, key.strip())
    elif settings().auth == "local":
        user = await local_owner(s)
    else:
        raise Problem(
            401, "Sign in with an API key: send it as the header Authorization: Bearer <key>."
        )
    if user.disabled_at is not None:
        raise Problem(403, "This account is turned off. Ask whoever runs the studio about it.")
    return user
