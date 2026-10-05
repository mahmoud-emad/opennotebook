"""The worker: runs the queue, and recovers work a dead worker left behind.

`opennotebook worker` runs this. Builds take the `prep` lock, so however many
jobs the worker runs at once, one build runs at a time; research and the
rest run beside it.
"""

import asyncio
import contextlib
import logging

from procrastinate.jobs import Status
from sqlalchemy import update

from opennotebook.db.models import Job
from opennotebook.db.session import sessionmaker
from opennotebook.jobs import CANCELLED, FAILED, LIVE, QUEUED, notify, now
from opennotebook.jobs.app import app

log = logging.getLogger(__name__)

# How often a running worker looks for work a dead one left behind.
RECOVER_EVERY_SECONDS = 60

# A job whose worker has not been heard from in this long is stalled. The
# worker writes its heartbeat every ten seconds.
STALLED_AFTER_SECONDS = 60

# A stalled job is run again this many times at most; after that it is
# failed, so a build that kills its worker every time does not loop forever.
MAX_RECOVERIES = 1

INTERRUPTED = (
    "The studio restarted before this finished, and it could not be picked up again. Try again."
)


async def recover_stalled(seconds: float = STALLED_AFTER_SECONDS) -> int:
    """Put work whose worker died back on the queue, once; fail it after
    that. Replaces the Rust server's `recover_orphans`: a prep whose process
    died is not left saying `running`. Returns how many jobs were handled."""
    handled = 0
    for job in await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=seconds):
        handled += 1
        assert job.id is not None
        if job.attempts <= MAX_RECOVERIES:
            log.warning(
                "job %s (%s) was left by a dead worker; running it again", job.id, job.task_name
            )
            await app.job_manager.retry_job(job)
            async with sessionmaker()() as s, s.begin():
                ours = await s.execute(
                    update(Job)
                    .where(Job.procrastinate_job_id == job.id, Job.status.in_(LIVE))
                    .values(status=QUEUED, step="", steps_done=0)
                    .returning(Job.id)
                )
                for (jid,) in ours:
                    await notify(s, jid)
            continue
        log.warning(
            "job %s (%s) was left by a dead worker again; failing it", job.id, job.task_name
        )
        await app.job_manager.finish_job(job, status=Status.FAILED, delete_job=False)
        async with sessionmaker()() as s, s.begin():
            ours = await s.execute(
                update(Job)
                .where(Job.procrastinate_job_id == job.id, Job.status.not_in((CANCELLED,)))
                .values(status=FAILED, error=INTERRUPTED, finished_at=now())
                .returning(Job.id)
            )
            for (jid,) in ours:
                await notify(s, jid)
    return handled


async def _recover_forever() -> None:
    while True:
        try:
            await recover_stalled()
        except Exception:
            log.exception("could not look for stalled jobs")
        await asyncio.sleep(RECOVER_EVERY_SECONDS)


async def run(concurrency: int = 4) -> None:
    """Run the worker until it is stopped."""
    async with app.open_async():
        recovering = asyncio.create_task(_recover_forever())
        try:
            await app.run_worker_async(concurrency=concurrency, install_signal_handlers=True)
        finally:
            recovering.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovering
