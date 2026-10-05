"""Collections: a person's sets of sources, and the counts of what they hold.

Every function takes the owner first and touches only that owner's rows.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, Text, cast, column, delete, exists, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from opennotebook import cover, jobs
from opennotebook.db.models import (
    Collection,
    MindMap,
    Session,
    Share,
    Source,
    StudyNotes,
    User,
)
from opennotebook.domain import settings as st
from opennotebook.errors import Problem, not_found

# More empty collections than this and starting another is refused, whoever
# asks: the page, the chat or an agent.
MAX_EMPTY_COLLECTIONS = 5

# What an untitled collection is called wherever its name is shown.
UNTITLED = "Untitled collection"

# The queue's own table, for whether a collection's naming and cover are
# still to be made: its refresh job is waiting or running. Read, never
# written, so a light table construct rather than a model.
_queue = table("procrastinate_jobs", column("lock"), column("status"))


def display_title(title: str) -> str:
    """A collection's name as it is shown: its title, or "Untitled
    collection" while it has none."""
    return " ".join(title.split()) or UNTITLED


@dataclass
class Summary:
    collection: Collection
    sources: int
    decks: int
    audios: int
    maps: int
    notes: int
    preparing: int
    failed: int
    # The version of the cover drawn now: the designed one while covers are
    # on, else the one drawn from the cid, title and source names (`f-…`).
    cover_version: str = ""
    # The collection has a share, so everyone on the studio sees it in Discover.
    shared: bool = False
    # How many times its share was reused; 0 when it has none.
    reuses: int = 0
    # The title of the collection a reuse copied it from, while that one is
    # still shared; None when it was not reused or the share is gone.
    reused_title: str | None = None
    # Its name or cover is still to be made: a refresh is waiting or running.
    refreshing: bool = False
    # The studio names it from its sources: automatic naming is on and nobody
    # has given it a name.
    auto_named: bool = False

    @property
    def busy(self) -> bool:
        """Something in it is still being made: an output, its name or its
        cover. A page showing it should keep listening."""
        return self.preparing > 0 or self.refreshing

    @property
    def empty(self) -> bool:
        return not (self.sources or self.decks or self.audios or self.maps or self.notes)


def refuse_another(summaries: list[Summary]) -> str | None:
    """Why another collection may not be started, when it may not."""
    empty = sum(1 for c in summaries if c.empty)
    if empty < MAX_EMPTY_COLLECTIONS:
        return None
    return (
        f"You already have {empty} empty collections. Add sources to one of them, "
        "or delete the ones you don't need, before starting another."
    )


def _count(model: type[Source | MindMap | StudyNotes]) -> Select[int]:
    return select(func.count()).where(model.collection_id == Collection.id)


def _sessions(*where: object) -> Select[int]:
    return select(func.count()).where(Session.collection_id == Collection.id, *where)  # pyright: ignore[reportArgumentType]


def _names() -> Select[Sequence[str]]:
    return select(func.array_agg(Source.name)).where(Source.collection_id == Collection.id)


Row = tuple[
    Collection,
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    Sequence[str] | None,
    int | None,
    str | None,
    bool,
]


def _counted() -> Select[
    Collection, int, int, int, int, int, int, int, Sequence[str], int, str, bool
]:
    original = aliased(Collection)
    return select(
        Collection,
        _count(Source).scalar_subquery(),
        _sessions(Session.kind == "slides").scalar_subquery(),
        _sessions(Session.kind == "audio").scalar_subquery(),
        _count(MindMap).scalar_subquery(),
        _count(StudyNotes).scalar_subquery(),
        _sessions(Session.state == "preparing").scalar_subquery(),
        _sessions(Session.state == "failed").scalar_subquery(),
        _names().scalar_subquery(),
        # Its share's reuses; null when it has no share.
        select(Share.reuses).where(Share.collection_id == Collection.id).scalar_subquery(),
        # The title of the collection it was reused from, while that is shared.
        select(original.title)
        .join(Share, Share.collection_id == original.id)
        .where(Share.id == Collection.reused_from)
        .scalar_subquery(),
        # Its refresh is waiting or running (`refresh_lock` names the lock).
        exists().where(
            _queue.c.lock == func.concat("refresh:", cast(Collection.id, Text)),
            cast(_queue.c.status, Text).in_(("todo", "doing")),
        ),
    )


def _summaries(
    owner: uuid.UUID,
) -> Select[Collection, int, int, int, int, int, int, int, Sequence[str], int, str, bool]:
    return (
        _counted()
        .where(Collection.owner_id == owner)
        .order_by(Collection.updated_at.desc(), Collection.id.desc())
    )


def _summary(row: Row, covers_on: bool, naming_on: bool = False) -> Summary:
    c, sources, decks, audios, maps, notes, preparing, failed, names, reuses = row[:10]
    reused_title, refreshing = row[10], row[11]
    version = cover.current_version(str(c.id), c.title, c.cover, covers_on, sorted(names or []))
    return Summary(
        c,
        sources,
        decks,
        audios,
        maps,
        notes,
        preparing,
        failed,
        version,
        shared=reuses is not None,
        reuses=reuses or 0,
        reused_title=reused_title,
        refreshing=bool(refreshing),
        auto_named=naming_on and c.title_auto,
    )


async def _covers_on(s: AsyncSession, owner: uuid.UUID) -> bool:
    return st.is_on(await st.value(s, owner, st.COVERS_KEY))


async def _naming_on(s: AsyncSession, owner: uuid.UUID) -> bool:
    return st.is_on(await st.value(s, owner, st.AUTO_NAME_KEY))


async def list_all(s: AsyncSession, owner: uuid.UUID) -> list[Summary]:
    rows = await s.execute(_summaries(owner))
    covers_on, naming_on = await _covers_on(s, owner), await _naming_on(s, owner)
    return [_summary(row, covers_on, naming_on) for row in rows]


async def summary(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> Summary:
    row = (await s.execute(_summaries(owner).where(Collection.id == cid))).first()
    if row is None:
        raise not_found("That collection")
    return _summary(row, await _covers_on(s, owner), await _naming_on(s, owner))


async def of_anyone(s: AsyncSession, cids: list[uuid.UUID]) -> dict[uuid.UUID, Summary]:
    """The summaries of `cids` whoever owns them, each with its cover as its
    owner sees it. Only for what a share shows of a collection to everyone;
    every other read goes through the owner."""
    if not cids:
        return {}
    rows = list(await s.execute(_counted().where(Collection.id.in_(cids))))
    covers_on = {o: await _covers_on(s, o) for o in {row[0].owner_id for row in rows}}
    return {row[0].id: _summary(row, covers_on[row[0].owner_id]) for row in rows}


async def lock(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> Collection:
    """The collection, locked until the transaction ends. Every write to it
    or to what it holds takes this first, in the api and the worker alike."""
    c = await s.scalar(
        select(Collection)
        .where(Collection.id == cid, Collection.owner_id == owner)
        .with_for_update()
    )
    if c is None:
        raise not_found("That collection")
    return c


async def read_only(s: AsyncSession, c: Collection) -> Problem:
    """Why a read-only copy refuses a change: named after the collection it
    was copied from, as that one is called now, while its share is there."""
    original = None
    if c.reused_from is not None:
        original = await s.scalar(
            select(Collection.title)
            .join(Share, Share.collection_id == Collection.id)
            .where(Share.id == c.reused_from)
        )
    title = " ".join((original or "").split())
    of = f"“{title}”" if title else "a shared collection"
    return Problem(
        403,
        f"This is a read-only copy of {of}. Its author did not allow edits, "
        "so it cannot be changed or shared. Start a new collection to make one of your own.",
    )


async def refuse_read_only(s: AsyncSession, c: Collection) -> None:
    """Refuse a change to a read-only copy. Its being read-only is set when
    it is made and never changes, so this needs no lock."""
    if c.read_only:
        raise await read_only(s, c)


async def editable(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> Collection:
    """One of the owner's collections that may be changed: not there is not
    found, a read-only copy is refused. Every route that adds to a collection,
    changes or removes what it holds, or shares it, asks this first; reading,
    asking, pinning and deleting the whole collection do not."""
    c = await s.scalar(select(Collection).where(Collection.id == cid, Collection.owner_id == owner))
    if c is None:
        raise not_found("That collection")
    await refuse_read_only(s, c)
    return c


async def create(s: AsyncSession, owner: uuid.UUID, title: str) -> Collection:
    """Start a collection. An empty title leaves the naming to the studio."""
    # The owner's row is the lock that makes the count and the insert one step,
    # so two quick clicks cannot both get past the limit.
    await s.execute(select(User.id).where(User.id == owner).with_for_update())
    if why := refuse_another(await list_all(s, owner)):
        raise Problem(409, why)
    title = " ".join(title.split())
    c = Collection(owner_id=owner, title=title, title_auto=not title)
    s.add(c)
    await s.flush()
    await s.refresh(c)
    return c


async def retitle(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, title: str) -> None:
    """Rename a collection; an empty title hands the naming back to the studio."""
    c = await lock(s, owner, cid)
    await refuse_read_only(s, c)
    title = " ".join(title.split())
    c.title_auto = not title
    if title:
        c.title = title


async def pin(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, pinned: bool) -> None:
    c = await lock(s, owner, cid)
    c.pinned = pinned


async def remove(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> None:
    """Delete a collection and everything made from it. The rows go with it
    through their foreign keys; files on the volume are removed by the caller
    after the commit."""
    await lock(s, owner, cid)
    await s.execute(delete(Collection).where(Collection.id == cid, Collection.owner_id == owner))


async def touch(s: AsyncSession, cid: uuid.UUID) -> None:
    """A source or an output was added or changed: the pages following the
    collection hear of it once `s` commits."""
    await s.execute(
        update(Collection).where(Collection.id == cid).values(updated_at=datetime.now(UTC))
    )
    await jobs.announce(s, cid)
