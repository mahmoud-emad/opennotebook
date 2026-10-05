"""What the api tells the people following an output: one `LISTEN`
connection, fanned out to every open event stream.

The worker announces each write to a job row with
`pg_notify('job_progress', <job id>)`, a change to a playhead is
announced with `pg_notify('session_change', <output id>)`, and a change to
a collection's title, cover or sources with
`pg_notify('collection_change', <collection id>)`. Neither payload
carries the change itself: a stream that wakes reads the rows, so a
notification that is missed costs a moment, never a state. A stream also
reads the rows when it starts and every `REREAD_SECONDS` without a wake-up,
so a client that connects late, or a listener that dropped its connection,
still sees where things are.

A stream is told why it woke (`Wake.clear`), so it reads only what may have
changed: a playhead that moved is the playback row, not the whole output;
a job's report is that job's row, not the whole collection.

A job's report wakes only the streams that follow what the job is about.
The hub learns that when a stream reads its rows (`follow_job`), and for a
job it has not seen, from the job's own row, read once and remembered. What
it remembers is bounded (`JOBS_MAX`), the oldest forgotten first.

The listener is a direct psycopg connection, not a pooled one: `LISTEN`
does not survive a transaction-mode pooler.
"""

import asyncio
import contextlib
import logging
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import AsyncGenerator

import psycopg
from sqlalchemy import select

from opennotebook.jobs import CHANNEL, COLLECTION_CHANNEL
from opennotebook.jobs.app import conninfo

log = logging.getLogger(__name__)

SESSION_CHANNEL = "session_change"

# How long a quiet stream waits before it reads the rows anyway; also its
# keep-alive, inside the usual 30–60 s idle timeouts of proxies.
REREAD_SECONDS = 15.0

# Why a stream woke: a job it follows reported, its output's playhead moved,
# or its collection changed.
JOB, SESSION, COLLECTION = "job", "session", "collection"

# How many jobs the hub remembers what they are about.
JOBS_MAX = 4096


class Wake:
    """What a stream waits on: set when what it follows may have changed,
    with why."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._why: set[str] = set()

    def set(self, why: str) -> None:
        self._why.add(why)
        self._event.set()

    def clear(self) -> set[str]:
        """Start waiting again; why it woke since the last time."""
        why, self._why = self._why, set()
        self._event.clear()
        return why

    async def wait(self) -> None:
        await self._event.wait()


class Hub:
    """Wakes the streams that follow an output or a collection when its
    rows change."""

    def __init__(self) -> None:
        self._waiters: dict[uuid.UUID, set[Wake]] = defaultdict(set)
        # What each job's reports are about (its output, its collection),
        # most recently used last.
        self._jobs: OrderedDict[uuid.UUID, set[uuid.UUID]] = OrderedDict()
        # Jobs whose row is being read to learn what they are about.
        self._routing: set[uuid.UUID] = set()
        self._routes: set[asyncio.Task[None]] = set()
        self._task: asyncio.Task[None] | None = None
        # Whether the `LISTEN` connection is up, and why it last failed while
        # it is not: what readiness reports.
        self.connected = False
        self.failure: str | None = None

    def follow_job(self, job_id: uuid.UUID, key: uuid.UUID) -> None:
        """Wake the streams that follow `key` (an output or a collection)
        when the job reports."""
        self._remember(job_id, {key})

    def _remember(self, job_id: uuid.UUID, keys: set[uuid.UUID]) -> None:
        about = self._jobs.setdefault(job_id, set())
        about.update(keys)
        self._jobs.move_to_end(job_id)
        while len(self._jobs) > JOBS_MAX:
            self._jobs.popitem(last=False)

    @contextlib.asynccontextmanager
    async def subscribe(self, sid: uuid.UUID) -> AsyncGenerator[Wake]:
        """Set whenever `sid` may have changed, with why, for as long as the
        caller holds it."""
        self._ensure()
        w = Wake()
        self._waiters[sid].add(w)
        try:
            yield w
        finally:
            self._waiters[sid].discard(w)
            if not self._waiters[sid]:
                del self._waiters[sid]

    def wake(self, sid: uuid.UUID, why: str = COLLECTION) -> None:
        for w in self._waiters.get(sid, ()):
            w.set(why)

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
                    await conn.execute(f"LISTEN {COLLECTION_CHANNEL}")
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
            self.wake(key, SESSION)
        elif channel == COLLECTION_CHANNEL:
            self.wake(key, COLLECTION)
        elif (keys := self._jobs.get(key)) is not None:
            self._jobs.move_to_end(key)
            for k in list(keys):
                self.wake(k, JOB)
        elif self._waiters and key not in self._routing:
            # A job no stream has read yet: its row says what it is about,
            # read once, and only the streams that follow that are woken.
            self._routing.add(key)
            task = asyncio.get_running_loop().create_task(self._route(key))
            self._routes.add(task)
            task.add_done_callback(self._routes.discard)

    async def _route(self, job_id: uuid.UUID) -> None:
        from opennotebook.db.models import Job
        from opennotebook.db.session import sessionmaker

        try:
            async with sessionmaker()() as s:
                row = (
                    await s.execute(
                        select(Job.session_id, Job.collection_id).where(Job.id == job_id)
                    )
                ).first()
        except Exception as e:
            log.warning("could not learn what job %s is about (%s)", job_id, e)
            return
        finally:
            self._routing.discard(job_id)
        keys = {k for k in (row or ()) if k is not None}
        self._remember(job_id, keys)
        for k in keys:
            self.wake(k, JOB)

    @property
    def listening(self) -> bool:
        """Whether a stream is following anything, so the listener should be
        connected."""
        return self._task is not None and not self._task.done()

    async def close(self) -> None:
        for t in list(self._routes):
            t.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


hub = Hub()
