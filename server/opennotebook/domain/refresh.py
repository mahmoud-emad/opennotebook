"""Naming a collection and designing its cover, in the background, merged
per collection.

Ported from `opennotebook_server/src/collection.rs` (`refresh`, `run_claim`,
`run_done`, `enqueue_covers`). For now the work runs as asyncio tasks in the
api process; the work itself is `naming.name` and `covers.redraw`, plain
functions that move into Procrastinate jobs in phase 3.

A change is scheduled with `after_commit`, so the task starts only once the
change it is about is committed and visible to the task's own session, and
not at all if the request rolled back.
"""

import asyncio
import logging
import uuid
from collections import deque
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.domain import covers, naming

log = logging.getLogger(__name__)

# The collections being refreshed now, each with whether it changed again
# while it was. A change during a run does not start a second run beside it;
# it asks the running one to go round once more when it is done.
_runs: dict[uuid.UUID, bool] = {}

# Every task started here, held so none is collected while it runs and so
# `settle` can wait for them.
_tasks: set[asyncio.Task[None]] = set()


def run_claim(running: dict[uuid.UUID, bool], cid: uuid.UUID) -> bool:
    """Claim the run of `cid`: true when this caller should run it, false
    when a run is already going, which is told to run again."""
    if cid in running:
        running[cid] = True
        return False
    running[cid] = False
    return True


def run_done(running: dict[uuid.UUID, bool], cid: uuid.UUID) -> bool:
    """A run of `cid` finished: true when it must go round again."""
    if running.get(cid):
        running[cid] = False
        return True
    running.pop(cid, None)
    return False


async def refresh(owner: uuid.UUID, cid: uuid.UUID) -> None:
    """Name the collection, then design its cover, merged per collection. Each
    step skips itself when what it was made from has not changed, so a run
    costs a model call only for what is new."""
    if not run_claim(_runs, cid):
        return
    try:
        while True:
            try:
                await naming.name(owner, cid)
            except Exception:
                log.exception("collection %s: not named", cid)
            try:
                await covers.redraw(owner, cid, force=False)
            except Exception:
                log.exception("collection %s: cover not designed", cid)
            if not run_done(_runs, cid):
                break
    except BaseException:
        # Cancelled: free the claim, or no later change could run it again.
        _runs.pop(cid, None)
        raise


def _start(work: Any) -> None:
    task: asyncio.Task[None] = asyncio.get_running_loop().create_task(work)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def spawn(owner: uuid.UUID, cid: uuid.UUID) -> None:
    """Refresh the collection in the background. Never waits on the model."""
    _start(refresh(owner, cid))


def after_commit(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> None:
    """Refresh the collection once `s` commits: its sources changed."""

    def committed(_: Any) -> None:
        spawn(owner, cid)

    event.listen(s.sync_session, "after_commit", committed, once=True)


# Collections a list found with no cover for what they hold now, waiting for
# one worker. Small on purpose: a first list of many collections designs a few
# covers at a time, one after another, and the rest are found again by a
# later list.
BACKLOG_MAX = 3
_backlog: deque[tuple[uuid.UUID, uuid.UUID]] = deque()
_draining = False


def enqueue_covers(owner: uuid.UUID, cids: list[uuid.UUID]) -> None:
    """Queue covers to design, a few at most, with one worker draining them."""
    global _draining
    for cid in cids:
        if len(_backlog) >= BACKLOG_MAX:
            break
        if (owner, cid) not in _backlog:
            _backlog.append((owner, cid))
    if not _draining and _backlog:
        _draining = True
        _start(_drain())


async def _drain() -> None:
    global _draining
    try:
        while _backlog:
            owner, cid = _backlog.popleft()
            await refresh(owner, cid)
    finally:
        _draining = False


async def settle() -> None:
    """Wait until every background refresh has finished. For tests, and for a
    clean shutdown."""
    while _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
