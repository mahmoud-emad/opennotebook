"""Stopping the api cleanly: work still running is given a moment to end,
then stopped, and every connection the process holds is closed."""

import asyncio
import logging
from collections.abc import Set

log = logging.getLogger(__name__)

# How long running work is given to end when the api stops: inside the ten
# seconds a container is given before it is killed.
DRAIN_SECONDS = 8.0


async def finish[T](tasks: Set[asyncio.Task[T]], seconds: float) -> None:
    """Wait up to `seconds` for `tasks` to end, then cancel what is left and
    wait for it to unwind."""
    running = set(tasks)
    if not running:
        return
    _, left = await asyncio.wait(running, timeout=seconds)
    if left:
        log.warning("stopping %d task(s) still running at shutdown", len(left))
        for t in left:
            t.cancel()
        await asyncio.wait(left, timeout=seconds)


async def close_all() -> None:
    """Everything the api holds open: running chat turns and refreshes not
    yet queued, the progress listener, the AI client's connections and the
    database pool. Each is closed only if it was ever opened."""
    from opennotebook.ai.client import ai
    from opennotebook.api import chat
    from opennotebook.db.session import engine
    from opennotebook.domain import refresh
    from opennotebook.jobs.events import hub

    await chat.drain(DRAIN_SECONDS)
    await refresh.drain(DRAIN_SECONDS)
    await hub.close()
    if ai.cache_info().currsize:
        await ai().aclose()
    if engine.cache_info().currsize:
        await engine().dispose()
