"""What the build tests share: an empty queue after every test, and a way to
run the worker until the queue is drained."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from opennotebook.db.session import engine
from opennotebook.jobs.app import app
from opennotebook.jobs.events import hub


@pytest.fixture(autouse=True)
async def empty_queue() -> AsyncIterator[None]:
    yield
    await hub.close()
    async with engine().begin() as c:
        await c.execute(text("TRUNCATE procrastinate_jobs, procrastinate_workers CASCADE"))


async def drain(*queues: str, concurrency: int = 1) -> None:
    """Run the worker until nothing it reads is left to do."""
    async with app.open_async():
        # A job held back by another's lock is not fetched while that one
        # runs, so a single pass can leave it waiting: go round until none is.
        names = list(queues) or ["prep", "work", "refresh"]
        for _ in range(10):
            await app.run_worker_async(
                queues=names,
                wait=False,
                install_signal_handlers=False,
                listen_notify=False,
                concurrency=concurrency,
            )
            async with engine().begin() as c:
                left = await c.scalar(
                    text(
                        "SELECT count(*) FROM procrastinate_jobs "
                        "WHERE status = 'todo' AND queue_name = ANY(:q)"
                    ),
                    {"q": names},
                )
            if not left:
                return
