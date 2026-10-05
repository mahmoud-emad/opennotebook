"""What the api tells the people following an output: one `LISTEN`
connection, fanned out to every open event stream.

The worker announces each write to a job row with
`pg_notify('job_progress', <job id>)`, and a change to a playhead is
announced with `pg_notify('session_change', <output id>)`. Neither payload
carries the change itself: a stream that wakes reads the rows, so a
notification that is missed costs a moment, never a state. A stream also
reads the rows when it starts and every `REREAD_SECONDS` without a wake-up,
so a client that connects late, or a listener that dropped its connection,
still sees where things are.

The listener is a direct psycopg connection, not a pooled one: `LISTEN`
does not survive a transaction-mode pooler.
"""

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator

import psycopg

from opennotebook.jobs import CHANNEL
from opennotebook.jobs.app import conninfo

log = logging.getLogger(__name__)

SESSION_CHANNEL = "session_change"

# How long a quiet stream waits before it reads the rows anyway; also its
# keep-alive, inside the usual 30–60 s idle timeouts of proxies.
REREAD_SECONDS = 15.0


class Hub:
    """Wakes every stream that follows an output when its rows change."""

    def __init__(self) -> None:
        self._waiters: dict[uuid.UUID, set[asyncio.Event]] = defaultdict(set)
        # Which output a job belongs to, learnt when a stream subscribes.
        self._jobs: dict[uuid.UUID, uuid.UUID] = {}
        self._task: asyncio.Task[None] | None = None
        # Whether the `LISTEN` connection is up, and why it last failed while
        # it is not: what readiness reports.
        self.connected = False
        self.failure: str | None = None

    def follow_job(self, job_id: uuid.UUID, sid: uuid.UUID) -> None:
        self._jobs[job_id] = sid

    @contextlib.asynccontextmanager
    async def subscribe(self, sid: uuid.UUID) -> AsyncGenerator[asyncio.Event]:
        """An event set whenever `sid` may have changed, for as long as the
        caller holds it."""
        self._ensure()
        ev = asyncio.Event()
        self._waiters[sid].add(ev)
        try:
            yield ev
        finally:
            self._waiters[sid].discard(ev)
            if not self._waiters[sid]:
                del self._waiters[sid]

    def wake(self, sid: uuid.UUID) -> None:
        for ev in self._waiters.get(sid, ()):
            ev.set()

    def _ensure(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._listen())

    async def _listen(self) -> None:
        """Hold the `LISTEN` connection, reconnecting after a failure."""
        while self._waiters:
            try:
                async with await psycopg.AsyncConnection.connect(
                    conninfo(), autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    await conn.execute(f"LISTEN {SESSION_CHANNEL}")
                    self.connected, self.failure = True, None
                    try:
                        async for n in conn.notifies():
                            self._on(n.channel, n.payload)
                            if not self._waiters:
                                return
                    finally:
                        self.connected = False
            except (psycopg.Error, OSError) as e:
                log.warning("the progress listener lost its connection (%s); reconnecting", e)
                self.failure = str(e) or type(e).__name__
                await asyncio.sleep(1)

    def _on(self, channel: str, payload: str) -> None:
        try:
            key = uuid.UUID(payload)
        except ValueError:
            return
        if channel == SESSION_CHANNEL:
            self.wake(key)
        elif (sid := self._jobs.get(key)) is not None:
            self.wake(sid)
        else:
            # A job no stream has seen yet, a retry say: every stream reads
            # its rows again, which costs a query each and misses nothing.
            for sid in list(self._waiters):
                self.wake(sid)

    @property
    def listening(self) -> bool:
        """Whether a stream is following anything, so the listener should be
        connected."""
        return self._task is not None and not self._task.done()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


hub = Hub()
