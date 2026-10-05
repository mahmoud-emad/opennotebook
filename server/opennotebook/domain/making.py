"""Mind maps and study notes, made in the background.

Each is one model call of seconds to a minute, too long for a request to
wait on. Asking for one checks everything that can be checked at once (the
collection is there and may be changed, the sources are, the spending limit
allows it), then writes the output's row as `making` with the job that will
fill it, and puts the job on the queue, all in one transaction: the page
shows the row being made at once, and a request that fails leaves nothing.

The job reads the sources again, asks the model, and writes the row `ready`
under the collection's lock. When it fails, the row goes and the job says
why, in a sentence; when it is stopped, the row goes too. A row whose job
died without saying so (the worker was killed) is removed the next time the
collection's list is read (`settle`).

The chat agent and the old JSON-RPC API answer with the finished output, so
they run the same job in the request (`run`) rather than on the queue.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import jobs
from opennotebook.db.models import Job, MindMap, StudyNotes
from opennotebook.db.session import sessionmaker
from opennotebook.domain import collections
from opennotebook.errors import SERVER_FAULT, Problem
from opennotebook.jobs import NoJob, Progress

log = logging.getLogger(__name__)

Made = MindMap | StudyNotes

# The queue tasks that make each, by job kind.
MAP_TASK = "make_mindmap"
NOTES_TASK = "make_notes"

# One phase: the model call. What it is doing is the step's text.
PHASES = 1


def gone_while_made(what: str) -> Problem:
    return Problem(404, f"The collection was deleted while {what} being made, so nothing was kept.")


async def start[T: Made](
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, model: type[T], kind: str, **row: Any
) -> tuple[Job, T]:
    """Write a `making` row of `model` and the job that will fill it, under
    the collection's lock, in the caller's transaction; the collection's
    pages hear of it once that commits."""
    try:
        await collections.lock(s, owner, cid)
    except Problem as e:
        raise gone_while_made("it was") from e
    job = await jobs.create(s, owner, kind, collection_id=cid, steps_total=PHASES)
    made = model(owner_id=owner, collection_id=cid, state="making", job_id=job.id, **row)
    s.add(made)
    await s.flush()
    await s.refresh(made)
    await collections.touch(s, cid)
    return job, made


async def run(
    job_id: uuid.UUID,
    owner: uuid.UUID,
    cid: uuid.UUID,
    model: type[Made],
    made_id: uuid.UUID,
    make: Callable[[Callable[[str], Awaitable[None]]], Awaitable[dict[str, Any]]],
    what: str,
) -> Problem | None:
    """Fill a `making` row: `make` reads what it needs and asks the model,
    saying what it is doing through the callback it is given, and returns
    the row's columns. Returns None when the row was made, else why not: the
    job closes with its sentence."""
    progress = Progress(job_id, PHASES)
    try:
        await progress.start()
        values = await make(progress.phase)
        async with sessionmaker()() as s, s.begin():
            try:
                await collections.lock(s, owner, cid)
            except Problem as e:
                raise gone_while_made(f"{what} was") from e
            row = await s.scalar(
                select(model).where(model.id == made_id, model.state == "making").with_for_update()
            )
            if row is None:
                raise Problem(
                    404,
                    f"{what.capitalize()} was deleted while it was being made, so it was not kept.",
                )
            for k, v in values.items():
                setattr(row, k, v)
            row.state = "ready"
            await collections.touch(s, cid)
    except NoJob:
        # The collection was deleted, and the job row with it.
        return None
    except asyncio.CancelledError:
        await asyncio.shield(_drop(model, made_id, cid))
        await asyncio.shield(progress.cancelled())
        raise
    except Problem as e:
        await _failed(progress, model, made_id, cid, e.detail)
        return e
    except Exception:
        log.exception("%s %s could not be made", what, made_id)
        await _failed(progress, model, made_id, cid, SERVER_FAULT)
        return Problem(500, SERVER_FAULT)
    progress.done = PHASES
    with contextlib.suppress(NoJob):
        await progress.finish(None)
    return None


async def _failed(
    progress: Progress, model: type[Made], made_id: uuid.UUID, cid: uuid.UUID, why: str
) -> None:
    await _drop(model, made_id, cid)
    with contextlib.suppress(NoJob):
        await progress.finish(why)


async def _drop(model: type[Made], made_id: uuid.UUID, cid: uuid.UUID) -> None:
    """Remove a row that will not be made; the collection's pages hear of
    it."""
    async with sessionmaker()() as s, s.begin():
        gone = await s.execute(delete(model).where(model.id == made_id, model.state == "making"))
        if gone.rowcount:  # pyright: ignore[reportAttributeAccessIssue]
            await collections.touch(s, cid)


async def settle[T: Made](s: AsyncSession, rows: Sequence[T]) -> list[T]:
    """`rows` without the ones whose making died: a `making` row whose job
    is no longer being worked on will never be filled, so it is removed
    rather than shown as being made for good."""
    making = {r.job_id: r for r in rows if r.state == "making" and r.job_id is not None}
    orphans = [r for r in rows if r.state == "making" and r.job_id is None]
    if not making and not orphans:
        return list(rows)
    live = await jobs.alive(s, list(making))
    dead = [r for j, r in making.items() if j not in live] + orphans
    for r in dead:
        await s.execute(delete(type(r)).where(type(r).id == r.id, type(r).state == "making"))
    return [r for r in rows if r not in dead]
