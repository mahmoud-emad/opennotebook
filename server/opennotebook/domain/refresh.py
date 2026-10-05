"""Naming a collection and designing its cover, in the background, merged
per collection.

Ported from `opennotebook_server/src/collection.rs` (`refresh`, `run_claim`,
`run_done`, `enqueue_covers`). The Rust server ran it with `tokio::spawn` and
merged runs in an in-process map; here it is a Procrastinate task (plan §6),
so it runs in the worker, survives an api restart, and is merged by the
queue itself:

* the task's `lock` is the collection, so two runs of one collection never
  run side by side;
* its `queueing_lock` is the same, so at most one run waits behind a running
  one, and a change made while one waits joins it rather than adding another.

That is exactly what `run_claim` / `run_done` did: a change during a run does
not start a second run beside it, it asks for one more run once this one is
done.

A change is queued in the transaction that makes it, so the worker sees the
job only once the change it is about is committed and visible to it, and not
at all if the request rolled back.
"""

import asyncio
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import jobs
from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import covers, naming
from opennotebook.jobs.app import REFRESH_QUEUE, REFRESH_TASK, app, refresh_lock

log = logging.getLogger(__name__)


async def refresh(owner: uuid.UUID, cid: uuid.UUID) -> None:
    """Name the collection, then design its cover. Each step skips itself
    when what it was made from has not changed, so a run costs a model call
    only for what is new. Run by the worker; `schedule` is how to ask for
    it."""
    try:
        await naming.name(owner, cid)
    except Exception:
        log.exception("collection %s: not named", cid)
    try:
        await covers.redraw(owner, cid, force=False)
    except Exception:
        log.exception("collection %s: cover not designed", cid)


async def schedule(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> bool:
    """Refresh the collection once `s` commits: its sources changed. Never
    waits on the model. False when a run already waiting will cover it."""
    lock = refresh_lock(cid)
    pid = await jobs.enqueue(
        s,
        REFRESH_TASK,
        {"owner_id": str(owner), "collection_id": str(cid)},
        queue=REFRESH_QUEUE,
        lock=lock,
        queueing_lock=lock,
    )
    return pid is not None


async def request(owner: uuid.UUID, cid: uuid.UUID) -> bool:
    """`schedule`, for work that is not inside a transaction of its own: the
    worker, after research added a source, or the chat agent after a turn."""
    async with sessionmaker()() as s, s.begin():
        return await schedule(s, owner, cid)


# The `request`s `spawn` started and that have not queued yet, held so none is
# collected half way and so `settle` can wait for them.
_queueing: set[asyncio.Task[bool]] = set()


def spawn(owner: uuid.UUID, cid: uuid.UUID) -> None:
    """`request`, from code that cannot wait for it. All it waits for is a
    row going onto the queue; the naming and the cover are the worker's."""
    task = asyncio.get_running_loop().create_task(request(owner, cid))
    _queueing.add(task)
    task.add_done_callback(_queueing.discard)


# Collections a list found with no cover for what they hold now. Small on
# purpose: a first list of many collections designs a few covers, and the
# rest are found again by a later list. A collection already waiting is not
# counted twice, so a page listed over and over adds nothing.
BACKLOG_MAX = 3


async def enqueue_covers(s: AsyncSession, owner: uuid.UUID, cids: list[uuid.UUID]) -> None:
    """Queue covers to design, a few at most."""
    for cid in cids[:BACKLOG_MAX]:
        await schedule(s, owner, cid)


async def settle() -> None:
    """Run every waiting refresh until none is left. For tests: in
    production the worker does this."""
    while _queueing:
        await asyncio.gather(*_queueing, return_exceptions=True)
    for _ in range(20):
        async with engine().connect() as c:
            left = await c.scalar(
                text(
                    "SELECT count(*) FROM procrastinate_jobs "
                    "WHERE queue_name = :q AND status = 'todo'"
                ),
                {"q": REFRESH_QUEUE},
            )
        if not left:
            return
        async with app.open_async():
            await app.run_worker_async(
                queues=[REFRESH_QUEUE],
                wait=False,
                install_signal_handlers=False,
                listen_notify=False,
                concurrency=1,
            )
    raise RuntimeError("refresh jobs were still waiting after twenty passes")
