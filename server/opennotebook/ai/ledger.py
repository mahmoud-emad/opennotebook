"""The spend ledger: every paid call is written to `usage_events`, against
the person it was made for and what it was for.

A scope is opened around a piece of work (a chat turn, a build, a map) with
`spending(...)`; every model call inside it, however deep, adds to it through
a context variable, as the task-local ledger did in Rust
(`session/src/spend.rs`). A call made outside any scope is a bug: it is
logged, and nobody is charged.
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal

from opennotebook.db.models import UsageEvent
from opennotebook.db.session import sessionmaker

log = logging.getLogger(__name__)


@dataclass
class Spend:
    owner_id: uuid.UUID
    kind: str
    collection_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    total_usd: Decimal = field(default_factory=Decimal)
    # False once any call in the scope could not be priced.
    known: bool = True
    calls: int = 0


_current: ContextVar[Spend | None] = ContextVar("spend", default=None)


@asynccontextmanager
async def spending(
    owner_id: uuid.UUID,
    kind: str,
    *,
    collection_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> AsyncGenerator[Spend]:
    spend = Spend(owner_id, kind, collection_id, session_id, job_id)
    token = _current.set(spend)
    try:
        yield spend
    finally:
        _current.reset(token)


def current() -> Spend | None:
    return _current.get()


async def record(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None,
    priced_by: str,
    speech_chars: int = 0,
    speech_seconds: float = 0.0,
) -> None:
    """Write one paid call. Its own transaction: the money was spent whether or
    not the work around it goes on to succeed."""
    spend = _current.get()
    if spend is None:
        log.error("a call to %s was made outside a spending scope; it is not recorded", model)
        return
    cost = None if cost_usd is None else Decimal(str(cost_usd))
    spend.calls += 1
    if cost is None:
        spend.known = False
    else:
        spend.total_usd += cost
    async with sessionmaker()() as s, s.begin():
        s.add(
            UsageEvent(
                owner_id=spend.owner_id,
                kind=spend.kind,
                collection_id=spend.collection_id,
                session_id=spend.session_id,
                job_id=spend.job_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                speech_chars=speech_chars,
                speech_seconds=speech_seconds,
                cost_usd=cost,
                priced_by=priced_by,
            )
        )
