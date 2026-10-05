"""The database engine, and the unit of work every request and job runs in.

A request's work is committed before its answer is sent (`db`), so a 2xx
always means it is saved, and what runs after the answer (removing files)
runs only once it is. Work that waits on something other than the database,
a model or a web page, lets its transaction go first (`release`) and writes
in a short transaction after, so no connection or row lock is held while it
waits.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opennotebook.config import Settings, settings


def server_options(c: Settings) -> str:
    """The limits Postgres enforces on every connection, as libpq `options`."""

    def ms(seconds: float) -> int:
        return max(int(seconds * 1000), 0)

    return (
        f"-c statement_timeout={ms(c.db_statement_timeout_s)} "
        f"-c idle_in_transaction_session_timeout={ms(c.db_idle_in_transaction_timeout_s)}"
    )


@lru_cache
def engine() -> AsyncEngine:
    c = settings()
    return create_async_engine(
        c.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=c.db_pool_size,
        max_overflow=c.db_max_overflow,
        pool_timeout=c.db_pool_timeout_s,
        connect_args={"options": server_options(c)},
    )


@lru_cache
def sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


@asynccontextmanager
async def unit() -> AsyncGenerator[AsyncSession]:
    """A session whose work is committed when the block ends and rolled back
    when it raises. Unlike `s.begin()`, the block may `release` part way
    through: what follows starts a new transaction, committed at the end the
    same way."""
    async with sessionmaker()() as s:
        try:
            yield s
        except BaseException:
            await s.rollback()
            raise
        await s.commit()


async def db() -> AsyncIterator[AsyncSession]:
    """A FastAPI dependency: one unit of work per request. Declared with
    `scope="function"` (`api.deps.Db`), so it is committed before the answer
    is sent, not after."""
    async with unit() as s:
        yield s


async def release(s: AsyncSession) -> None:
    """Commit what `s` has done so far and hand its connection back, before
    work that waits on something other than the database: a model, a web
    page, a file. The next statement starts a new transaction, so whatever
    was checked before must be checked again under a lock before writing."""
    await s.commit()
