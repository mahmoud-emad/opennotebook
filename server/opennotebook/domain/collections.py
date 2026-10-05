"""Collections: a person's sets of sources, and the counts of what they hold.

Every function takes the owner first and touches only that owner's rows.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import cover
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


Row = tuple[Collection, int, int, int, int, int, int, int, Sequence[str] | None, int | None]


def _counted() -> Select[Collection, int, int, int, int, int, int, int, Sequence[str], int]:
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
    )


def _summaries(
    owner: uuid.UUID,
) -> Select[Collection, int, int, int, int, int, int, int, Sequence[str], int]:
    return (
        _counted()
        .where(Collection.owner_id == owner)
        .order_by(Collection.updated_at.desc(), Collection.id.desc())
    )


def _summary(row: Row, covers_on: bool) -> Summary:
    c, sources, decks, audios, maps, notes, preparing, failed, names, reuses = row
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
    )


async def _covers_on(s: AsyncSession, owner: uuid.UUID) -> bool:
    return st.is_on(await st.value(s, owner, st.COVERS_KEY))


async def list_all(s: AsyncSession, owner: uuid.UUID) -> list[Summary]:
    rows = await s.execute(_summaries(owner))
    covers_on = await _covers_on(s, owner)
    return [_summary(row, covers_on) for row in rows]


async def summary(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> Summary:
    row = (await s.execute(_summaries(owner).where(Collection.id == cid))).first()
    if row is None:
        raise not_found("That collection")
    return _summary(row, await _covers_on(s, owner))


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
    """A source or an output was added or changed."""
    await s.execute(
        update(Collection).where(Collection.id == cid).values(updated_at=datetime.now(UTC))
    )
