"""The queue's tasks for mind maps and study notes, and its housekeeping.

A map or a set of notes is one model call, run here so no request waits on
it (`domain/making.py`). Each task reads its arguments strictly, and one
whose arguments do not decode fails its job with that rather than doing
something else.

Housekeeping runs once an hour: the queue's own finished jobs are removed
after `QUEUE_KEEP_HOURS`, and our job rows after `JOBS_KEEP_DAYS`, apart from
the newest job of an output that failed, which still says why. The spend
ledger (`usage_events`) is never pruned: it is the record of what was spent.
"""

import logging
import uuid

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text

from opennotebook.db.session import sessionmaker
from opennotebook.domain import making
from opennotebook.jobs import Progress
from opennotebook.jobs.app import WORK_QUEUE, app

log = logging.getLogger(__name__)

TIDY_TASK = "tidy"

# How long finished work is kept: the queue's own record of it, and the job
# rows people read progress and failures from.
QUEUE_KEEP_HOURS = 24 * 7
JOBS_KEEP_DAYS = 30
# Rows removed in one statement, so a first prune of a big table never holds
# a long lock.
PRUNE_BATCH = 5_000

UNREADABLE = (
    "This work reached the worker in a form it could not read, so it was not started. "
    "Start it again; if it keeps happening, the api and the worker may be running different "
    "versions, so restart both."
)


class MakeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    owner_id: uuid.UUID
    collection_id: uuid.UUID
    made_id: uuid.UUID
    focus: str
    sources: list[str] | None


async def _spec(spec: dict[str, object]) -> MakeSpec | None:
    try:
        return MakeSpec.model_validate(spec)
    except ValidationError as e:
        try:
            jid = uuid.UUID(str(spec.get("job_id")))
        except ValueError:
            log.error("a job arrived with no readable job id: %s", e)
            return None
        log.error("job %s: its arguments did not decode: %s", jid, e)
        await Progress(jid, 0).finish(UNREADABLE)
        return None


@app.task(name=making.MAP_TASK, queue=WORK_QUEUE, pass_context=False)
async def make_mindmap(**spec: object) -> None:
    """Draw a mind map whose row is waiting as `making`."""
    if (r := await _spec(spec)) is None:
        return
    from opennotebook.api import mindmaps

    await mindmaps.fill(r.job_id, r.owner_id, r.collection_id, r.made_id, r.focus, r.sources)


@app.task(name=making.NOTES_TASK, queue=WORK_QUEUE, pass_context=False)
async def make_notes(**spec: object) -> None:
    """Write study notes whose row is waiting as `making`."""
    if (r := await _spec(spec)) is None:
        return
    from opennotebook.api import notes

    await notes.fill(r.job_id, r.owner_id, r.collection_id, r.made_id, r.focus, r.sources)


@app.periodic(cron="23 * * * *")
@app.task(name=TIDY_TASK, queue=WORK_QUEUE, pass_context=False)
async def tidy(timestamp: int) -> None:
    """Remove finished work old enough not to be looked at again."""
    await app.job_manager.delete_old_jobs(
        nb_hours=QUEUE_KEEP_HOURS,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )
    removed = await prune_jobs()
    if removed:
        log.info("removed %d finished job rows older than %d days", removed, JOBS_KEEP_DAYS)


async def prune_jobs(days: int = JOBS_KEEP_DAYS) -> int:
    """Remove our job rows that finished more than `days` ago, a batch at a
    time, keeping the newest job of each output that failed: its error is
    the detail the output's failure is shown with. Returns how many went."""
    removed = 0
    while True:
        async with sessionmaker()() as s, s.begin():
            gone = await s.execute(
                text(
                    "DELETE FROM jobs WHERE id IN ("
                    " SELECT j.id FROM jobs j"
                    " WHERE j.finished_at < now() - make_interval(days => :days)"
                    " AND j.status IN ('done', 'failed', 'cancelled')"
                    " AND NOT EXISTS (SELECT 1 FROM sessions o"
                    "  WHERE o.id = j.session_id AND o.state = 'failed')"
                    " LIMIT :batch)"
                ),
                {"days": days, "batch": PRUNE_BATCH},
            )
        n = gone.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        removed += n
        if n < PRUNE_BATCH:
            return removed
