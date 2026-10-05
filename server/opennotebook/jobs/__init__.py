"""Background work: our `jobs` rows, which clients read for progress, and
the Procrastinate queue that runs them. A port of the job half of
`opennotebook_build/src/job.rs`.

One row per piece of work. The api writes it and puts the work on the queue
in the same transaction (`defer`), so a worker never finds work whose row is
not there yet, and a request that rolls back leaves nothing queued. The
worker writes progress onto the same row (`Progress`) and announces each
write with `pg_notify('job_progress', <job id>)`; the payload is the id only,
because a notification is capped at 8 KB and fires on commit. The api's one
`LISTEN` connection reads the row and pushes it to whoever follows it.

`steps_total = 0` means "this job does not say", not "zero phases": a client
draws a determinate bar only when `steps_total > 0`, so `Progress` sets it
at once and never leaves it at zero.

Stopping is Procrastinate's abort, which arrives in an async task as
`CancelledError`.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.db.models import Job
from opennotebook.db.session import sessionmaker

# Our statuses. Procrastinate keeps its own, on its own table.
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
LIVE = (QUEUED, RUNNING)

CHANNEL = "job_progress"


def now() -> datetime:
    return datetime.now(UTC)


async def notify(s: AsyncSession, job_id: uuid.UUID) -> None:
    """Announce a change to a job row; delivered when `s` commits."""
    await s.execute(text("SELECT pg_notify(:c, :id)"), {"c": CHANNEL, "id": str(job_id)})


async def create(
    s: AsyncSession,
    owner: uuid.UUID,
    kind: str,
    *,
    collection_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    steps_total: int = 0,
) -> Job:
    job = Job(
        owner_id=owner,
        kind=kind,
        collection_id=collection_id,
        session_id=session_id,
        status=QUEUED,
        steps_total=steps_total,
    )
    s.add(job)
    await s.flush()
    await s.refresh(job)
    return job


async def defer(
    s: AsyncSession,
    job: Job,
    task: str,
    args: dict[str, Any],
    *,
    queue: str,
    lock: str | None = None,
) -> int:
    """Put `task` on the queue for `job`, in the caller's transaction.

    Procrastinate's own SQL function, so its trigger tells the workers once
    the transaction commits, and not at all if it rolls back.
    """
    pid = await s.scalar(
        text(
            "SELECT unnest(procrastinate_defer_jobs_v1(ARRAY[ROW(CAST(:queue AS varchar), "
            "CAST(:task AS varchar), 0, CAST(:lock AS text), NULL::text, CAST(:args AS jsonb), "
            "NULL::timestamptz)]::procrastinate_job_to_defer_v1[]))"
        ),
        {"queue": queue, "task": task, "lock": lock, "args": json.dumps(args)},
    )
    assert pid is not None
    job.procrastinate_job_id = int(pid)
    await notify(s, job.id)
    await s.flush()
    return int(pid)


async def stop(s: AsyncSession, session_id: uuid.UUID) -> list[uuid.UUID]:
    """Stop an output's work, wherever it is: waiting its turn or halfway
    through its slides. A job that already ended is left as it ended, so
    stopping an output that is not being made changes nothing. Returns the
    ids of the jobs stopped."""
    live = list(
        await s.scalars(
            select(Job).where(Job.session_id == session_id, Job.status.in_(LIVE)).with_for_update()
        )
    )
    for job in live:
        job.status, job.error, job.finished_at = CANCELLED, "stopped", now()
        if job.procrastinate_job_id is not None:
            # A job waiting its turn is cancelled; a running one is asked to
            # abort, which reaches its task as `CancelledError`.
            await s.execute(
                text("SELECT procrastinate_cancel_job_v1(:id, true, false)"),
                {"id": job.procrastinate_job_id},
            )
        await notify(s, job.id)
    return [j.id for j in live]


class NoJob(Exception):
    """Progress was written for a job row that does not exist: the work was
    started without `create`, or its row was removed with what it was for,
    and the progress it reports would reach nobody."""


class Progress:
    """The work's own handle on its job row."""

    def __init__(self, job_id: uuid.UUID, total: int) -> None:
        self.job_id = job_id
        self.total = total
        self.done = 0

    async def start(self) -> None:
        """Take the row over. `steps_total` is written at once rather than at
        the first phase, so a screen polling before phase one gets a
        determinate bar instead of the "does not report" zero."""
        await self._write(RUNNING, "", 0, None, started=True)

    async def phase(self, label: str) -> None:
        """Name the phase now running. Called on entry to a phase, so
        `steps_done` counts phases finished rather than phases started."""
        await self._write(RUNNING, label, self.done, None)

    async def phase_done(self, label: str) -> None:
        """Mark the phase that was running as finished. `steps_done` never
        exceeds `steps_total`, so a miscounted phase cannot produce a bar past
        its end."""
        self.done = min(self.done + 1, self.total)
        await self._write(RUNNING, label, self.done, None)

    async def finish(self, error: str | None) -> None:
        """Close the job. Reaching `steps_total` does not imply success, so
        the outcome is carried by `status` and `error`, which is where a
        reader is told to look for it."""
        await self._write(DONE if error is None else FAILED, "", self.done, error, ended=True)

    async def cancelled(self) -> None:
        """Close a job whose work was stopped. A row already closed keeps its
        outcome."""
        async with sessionmaker()() as s, s.begin():
            await s.execute(
                update(Job)
                .where(Job.id == self.job_id, Job.status.in_(LIVE))
                .values(status=CANCELLED, error="stopped", finished_at=now())
            )
            await notify(s, self.job_id)

    async def _write(
        self,
        status: str,
        step: str,
        done: int,
        error: str | None,
        *,
        started: bool = False,
        ended: bool = False,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "step": step,
            "steps_done": done,
            "steps_total": self.total,
            "error": error,
        }
        if started:
            values["started_at"] = now()
        if ended:
            values["finished_at"] = now()
        async with sessionmaker()() as s, s.begin():
            # A cancelled row stays cancelled: the stop was asked for, and
            # progress from work that has not seen it yet must not undo it.
            changed = await s.execute(
                update(Job).where(Job.id == self.job_id, Job.status != CANCELLED).values(**values)
            )
            if changed.rowcount == 0:  # pyright: ignore[reportAttributeAccessIssue]
                if await s.get(Job, self.job_id) is None:
                    raise NoJob(f"job {self.job_id} has no row to report progress to")
                return
            await notify(s, self.job_id)


async def status_of(job_id: uuid.UUID) -> str | None:
    async with sessionmaker()() as s:
        return await s.scalar(select(Job.status).where(Job.id == job_id))
