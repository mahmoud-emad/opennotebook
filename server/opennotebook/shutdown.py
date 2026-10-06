"""Stopping the api cleanly: work still running is given a moment to end,
then stopped, and every connection the process holds is closed."""

import asyncio
import logging
from collections.abc import Set

log = logging.getLogger(__name__)

# How long the api waits for its open connections to close when it stops,
# before it closes them itself. A page's live stream never ends on its own,
# and the clean-up below only starts once every connection is gone, so
# without a bound the api would never stop. A page reconnects by itself.
CONNECTIONS_SECONDS = 2
# How long running work is then given to end: inside the fifteen seconds the
# api's container is given before it is killed (deploy/compose.yaml).
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
    yet queued, the progress listener, the AI and speech clients'
    connections and the database pool. Each is closed only if it was ever
    opened."""
    from opennotebook.api import chat
    from opennotebook.domain import refresh
    from opennotebook.jobs.events import hub

    await chat.drain(DRAIN_SECONDS)
    await refresh.drain(DRAIN_SECONDS)
    await hub.close()
    await close_clients()


async def close_clients() -> None:
    """The connections a process keeps for its own outbound calls and its
    database: the AI client's, the shared speech clients' and the pool. The
    worker closes these when it stops; the api, after its own work."""
    from opennotebook import speech
    from opennotebook.ai.client import ai
    from opennotebook.db.session import engine

    if ai.cache_info().currsize:
        await ai().aclose()
    await speech.close_shared()
    if engine.cache_info().currsize:
        await engine().dispose()
