"""What a job says while it runs: lines for the page to show as a terminal.

Work does not pass a handle around to say something. `Progress.start` binds
its job to the running task (a context variable, which tasks it starts
inherit), and anything under it calls `say`, `ok`, `warn` or `bad`: a phase
beginning, a model asked, a scene recorded, a repair made. Outside a job,
the same calls do nothing, so the code that says things runs the same in a
test and in a one-off script.

A line is `{at, level, text}`: `at` is epoch milliseconds, `level` one of
`LEVELS`. The row keeps the newest `KEPT`, and the page is sent the newest
`SHOWN`. Saying is never allowed to fail the work: a line that cannot be
written is dropped.
"""

import asyncio
import json
import logging
import time
import uuid
from contextvars import ContextVar, Token
from typing import Any

from sqlalchemy import text

from opennotebook.db.session import sessionmaker

log = logging.getLogger(__name__)

# How a line reads: plain, a step beginning, something done, something that
# did not go to plan but did not stop the work, and something that failed.
LEVELS = ("info", "step", "ok", "warn", "bad")
# Lines kept on the row, and lines sent to a page.
KEPT = 400
SHOWN = 400
# The longest a line is, in characters: a log line is a sentence, not a dump.
WIDTH = 300

_job: ContextVar[uuid.UUID | None] = ContextVar("job_log", default=None)

# Lines are written one at a time per process, so they land as they were said.
_gate = asyncio.Lock()

_APPEND = text(
    """
    UPDATE jobs
    SET log = (CASE WHEN jsonb_array_length(log) >= :kept THEN log - 0 ELSE log END)
              || CAST(:line AS jsonb)
    WHERE id = :id AND status IN ('queued', 'running')
    """
)


def bind(job_id: uuid.UUID) -> Token[uuid.UUID | None]:
    """Say into `job_id`'s log from here on, in this task and the ones it starts."""
    return _job.set(job_id)


def unbind(token: Token[uuid.UUID | None]) -> None:
    _job.reset(token)


async def say(line: str, level: str = "info") -> None:
    """Add a line to the running job's log; nothing when there is none."""
    job_id = _job.get()
    if job_id is None or not line.strip():
        return
    body = " ".join(line.split())
    if len(body) > WIDTH:
        body = body[: WIDTH - 1] + "…"
    entry: dict[str, Any] = {
        "at": int(time.time() * 1000),
        "level": level if level in LEVELS else "info",
        "text": body,
    }
    try:
        from opennotebook.jobs import notify  # the package imports this module

        async with _gate, sessionmaker()() as s, s.begin():
            await s.execute(_APPEND, {"kept": KEPT, "line": json.dumps(entry), "id": job_id})
            await notify(s, job_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("a job's log line could not be written", exc_info=True)


async def step(line: str) -> None:
    await say(line, "step")


async def ok(line: str) -> None:
    await say(line, "ok")


async def warn(line: str) -> None:
    await say(line, "warn")


async def bad(line: str) -> None:
    await say(line, "bad")


def shown(lines: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The lines a page is sent: the newest `SHOWN`, as stored."""
    return list(lines or [])[-SHOWN:]
