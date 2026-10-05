"""The database engine and a session per request."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opennotebook.config import settings


@lru_cache
def engine() -> AsyncEngine:
    return create_async_engine(settings().sqlalchemy_url, pool_pre_ping=True)


@lru_cache
def sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


async def db() -> AsyncIterator[AsyncSession]:
    """A FastAPI dependency: one session per request, committed when the
    handler returns and rolled back when it raises."""
    async with sessionmaker()() as s, s.begin():
        yield s
