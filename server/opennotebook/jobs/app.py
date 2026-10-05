"""The Procrastinate app: the queue the worker reads.

The connection is made when the app is opened, not when this module is
imported, so importing the api (to write the OpenAPI document, say) needs no
database.
"""

from typing import Any

import procrastinate
from psycopg_pool import AsyncConnectionPool

from opennotebook.config import settings

# The queue builds go on. Every build also carries the lock `PREP_LOCK`, which
# Procrastinate never lets two running jobs share: one prep at a time, however
# many workers there are. A deck or an audio overview is minutes of model calls
# and synthesis, and two at once would only make both slower while doubling
# what is in flight.
PREP_QUEUE = "prep"
PREP_LOCK = "prep"
# Everything else that takes longer than a request: deep research.
WORK_QUEUE = "work"
# Naming a collection and designing its cover, after its sources change. A job
# per collection carries the collection's id as both its `lock` and its
# `queueing_lock`: never two runs of one collection at once, and at most one
# waiting behind a running one, which every further change joins.
REFRESH_QUEUE = "refresh"
REFRESH_TASK = "refresh"


def refresh_lock(cid: object) -> str:
    return f"refresh:{cid}"


def conninfo() -> str:
    """The database as psycopg wants it: a plain `postgresql://` URL."""
    return settings().sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _pool(**kwargs: Any) -> AsyncConnectionPool:
    return AsyncConnectionPool(conninfo(), **kwargs)


app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(pool_factory=_pool),
    import_paths=["opennotebook.jobs.tasks"],
)
