"""Sharing a collection with everyone on the studio, and reusing a share.

Ported from the Rust server's `share.rs`. A share is one row per collection:
whether its sources are included, which of its ready outputs are, and a note.
Output keys are `session:<id>`, `mindmap:<id>` and `notes:<id>`. An output
deleted after it was shared leaves its key behind; every reader here resolves
keys against what is there now and skips the ones that do not.

A share is a live view, not a frozen copy: its card carries the collection's
title and cover as they are now, and its page the sources and outputs as they
are now. Discover lists the shares of every person on the studio, and any
signed-in person may read what a share includes, through the share and only
through it: the collection's own routes stay its owner's. Only the owner can
change or stop a share.

Reusing a share copies what it includes into a new collection of the caller's
own that owns all of its rows and files: each source with its passages (so
search and ask answer on the copy without a model call), each deck or audio
overview under a fresh id with its deck and audio directories copied and every
path and id that named the old one rewritten, each map and set of notes under
a fresh id. Nothing in the copy points back at the original, so it plays on
after the original or the share is deleted.

A share says whether its copies are their reuser's to change (`allow_edits`).
When it does not, each copy is read-only: it is read, asked and played, but
nothing in it changes and it is not shared again. That is decided when the
copy is made; changing the share later leaves the copies made before as they
are.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Insert,
    Integer,
    Select,
    Text,
    and_,
    case,
    cast,
    column,
    event,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    true,
    union_all,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, defer
from sqlalchemy.sql.elements import TextClause

from opennotebook import cover, storage
from opennotebook.db.models import (
    Chunk,
    Collection,
    InstanceSetting,
    MindMap,
    QaPair,
    Session,
    Share,
    Source,
    StudyNotes,
    User,
)
from opennotebook.domain import collections, covers
from opennotebook.domain import settings as st
from opennotebook.errors import Problem, not_found

# The longest note a share keeps, in characters.
NOTE_MAX = 280

KeyKind = Literal["session", "mindmap", "notes"]
OutputKind = Literal["slides", "audio", "mindmap", "notes"]


def gone() -> Problem:
    return not_found("That shared collection")


def not_yours() -> Problem:
    return Problem(
        403,
        "Only the person who shared this collection can change or stop its share. "
        "Reuse it to make a copy of your own.",
    )


# ── keys ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Key:
    """One output a share names."""

    kind: KeyKind
    id: uuid.UUID

    @classmethod
    def parse(cls, raw: str) -> Key | None:
        kind, _, id_ = raw.strip().partition(":")
        if kind not in ("session", "mindmap", "notes"):
            return None
        try:
            return cls(kind, uuid.UUID(id_))  # pyright: ignore[reportArgumentType]
        except ValueError:
            return None

    def render(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass
class Holdings:
    """What a collection holds that a share may include: its source count,
    and the ids of its ready decks and audio overviews, maps and notes."""

    sources: int = 0
    sessions: set[uuid.UUID] = field(default_factory=set[uuid.UUID])
    maps: set[uuid.UUID] = field(default_factory=set[uuid.UUID])
    notes: set[uuid.UUID] = field(default_factory=set[uuid.UUID])

    def holds(self, key: Key) -> bool:
        match key.kind:
            case "session":
                return key.id in self.sessions
            case "mindmap":
                return key.id in self.maps
            case "notes":
                return key.id in self.notes


def validate(
    include_sources: bool, outputs: Sequence[str], note: str, held: Holdings
) -> tuple[bool, list[str], str]:
    """What a share was asked, checked against what the collection holds:
    whether sources are included, the output keys (deduplicated, in the order
    given) and the note, trimmed and cut to `NOTE_MAX`. Refused when a key is
    not one of the collection's ready outputs, or nothing would be shared."""
    keys: list[str] = []
    for raw in outputs:
        key = Key.parse(raw)
        if key is None:
            raise Problem(
                422,
                f"“{raw.strip()}” does not name an output. Name each one as session:<id>, "
                "mindmap:<id> or notes:<id>.",
            )
        if not held.holds(key):
            raise Problem(
                422,
                f"“{raw.strip()}” is not a ready output of this collection; only ready outputs "
                "can be shared. Reload the page and pick again.",
            )
        if (k := key.render()) not in keys:
            keys.append(k)
    if not (include_sources and held.sources > 0) and not keys:
        raise Problem(
            422, "There is nothing to share. Include the sources or at least one ready output."
        )
    return include_sources, keys, note.strip()[:NOTE_MAX].rstrip()


async def holdings(s: AsyncSession, cid: uuid.UUID) -> Holdings:
    held = (await live(s, [cid]))[cid]
    sources = await s.scalar(select(func.count()).where(Source.collection_id == cid))
    return Holdings(sources or 0, set(held.sessions), set(held.maps), set(held.notes))


# ── what a collection holds now ───────────────────────────────────────────────


@dataclass
class Out:
    """A ready output, as a share shows it."""

    kind: OutputKind
    id: uuid.UUID
    title: str
    created_at: datetime
    # Slides of a deck, chapters of an audio overview; 0 for a map or notes.
    parts: int = 0
    duration_ms: int = 0

    @property
    def key(self) -> str:
        return f"{'session' if self.kind in ('slides', 'audio') else self.kind}:{self.id}"


@dataclass
class Live:
    """A collection's ready decks and audio overviews, maps and notes."""

    sessions: dict[uuid.UUID, Out] = field(default_factory=dict[uuid.UUID, Out])
    maps: dict[uuid.UUID, Out] = field(default_factory=dict[uuid.UUID, Out])
    notes: dict[uuid.UUID, Out] = field(default_factory=dict[uuid.UUID, Out])


async def live(s: AsyncSession, cids: list[uuid.UUID]) -> dict[uuid.UUID, Live]:
    """What each of `cids` holds now, in three queries whatever their number."""
    out = {cid: Live() for cid in cids}
    if not cids:
        return out
    sessions = await s.execute(
        select(
            Session.collection_id,
            Session.id,
            Session.kind,
            Session.title,
            Session.created_at,
            func.jsonb_array_length(Session.slides),
            Session.duration_ms,
        ).where(Session.collection_id.in_(cids), Session.state == "ready")
    )
    for cid, id_, kind, title, at, parts, ms in sessions:
        out[cid].sessions[id_] = Out(
            "audio" if kind == "audio" else "slides", id_, title, at, parts, ms
        )
    maps = await s.execute(
        select(MindMap.collection_id, MindMap.id, MindMap.title, MindMap.created_at).where(
            MindMap.collection_id.in_(cids), MindMap.state == "ready"
        )
    )
    for cid, id_, title, at in maps:
        out[cid].maps[id_] = Out("mindmap", id_, title, at)
    notes = await s.execute(
        select(
            StudyNotes.collection_id, StudyNotes.id, StudyNotes.title, StudyNotes.created_at
        ).where(StudyNotes.collection_id.in_(cids), StudyNotes.state == "ready")
    )
    for cid, id_, title, at in notes:
        out[cid].notes[id_] = Out("notes", id_, title, at)
    return out


def included(outputs: Sequence[str], held: Live) -> list[Out]:
    """Resolve a share's keys against the collection as it is now: a deck or
    audio overview that is still there and ready, a map or notes still
    there. A key that resolves to nothing is skipped."""
    seen: set[str] = set()
    out: list[Out] = []
    for key in filter(None, map(Key.parse, outputs)):
        if key.render() in seen:
            continue
        seen.add(key.render())
        found = {"session": held.sessions, "mindmap": held.maps, "notes": held.notes}[key.kind].get(
            key.id
        )
        if found is not None:
            out.append(found)
    return out


# ── reading a share ───────────────────────────────────────────────────────────


@dataclass
class Card:
    """A share as the feed shows it. Counts are of what the share includes and
    is still there."""

    id: uuid.UUID
    collection_id: uuid.UUID
    owner_id: uuid.UUID
    # The sharer's display name; empty when they have none.
    shared_by: str
    # The collection's current title.
    title: str
    note: str
    cover_version: str
    # The cover's terms; the feed's search also matches the cover's topic.
    terms: list[str]
    reuses: int
    # Copies made from it now are their reuser's to change and share again.
    allow_edits: bool
    created_at: datetime
    updated_at: datetime
    # 0 when sources are not included.
    sources: int = 0
    decks: int = 0
    audios: int = 0
    maps: int = 0
    notes: int = 0


def card(
    share: Share,
    shared_by: str,
    summary: collections.Summary,
    inc: list[Out],
    spec: cover.CoverSpec,
) -> Card:
    """A share as the feed shows it, from what it includes that is still there."""
    return Card(
        id=share.id,
        collection_id=share.collection_id,
        owner_id=share.owner_id,
        shared_by=shared_by,
        title=summary.collection.title,
        note=share.note,
        cover_version=summary.cover_version,
        terms=list(spec.terms),
        reuses=share.reuses,
        allow_edits=share.allow_edits,
        created_at=share.created_at,
        updated_at=share.updated_at,
        sources=summary.sources if share.include_sources else 0,
        decks=sum(1 for o in inc if o.kind == "slides"),
        audios=sum(1 for o in inc if o.kind == "audio"),
        maps=sum(1 for o in inc if o.kind == "mindmap"),
        notes=sum(1 for o in inc if o.kind == "notes"),
    )


Sort = Literal["newest", "reused"]

# The most cards one page of the feed holds, and how many it holds unless
# asked for fewer.
FEED_MAX = 200
FEED_DEFAULT = 100

# Whether a share matches a search, in the database: the search, trimmed and
# lowered (`:needle`), is a substring of the collection's title, the share's
# note, or the cover's topic or terms, the cover as its owner sees it. While
# the owner's covers are on and one is designed, that is the designed topic
# and terms; otherwise the title (or `Untitled collection`) and the titles of
# its first sources by name, which is what a cover not designed yet is drawn
# from. `strpos` rather than `LIKE`, so `%` and `_` are searched for as they
# are. `:covers_env` is the operator's override of the covers setting, empty
# when none; `:covers_default` what applies to a person with no row of their
# own.
_COVERS_ON = """(CASE WHEN :covers_env <> '' THEN :covers_env <> 'off'
  ELSE coalesce(nullif(btrim((SELECT u.value FROM user_settings u
    WHERE u.owner_id = shares.owner_id AND u.key = :covers_key AND :covers_user)), ''),
    :covers_default) <> 'off' END)"""
_DESIGNED = f"({_COVERS_ON} AND jsonb_typeof(collections.cover -> 'topic') = 'string')"
SEARCH = text(
    f"""(strpos(lower(collections.title), :needle) > 0
  OR strpos(lower(shares.note), :needle) > 0
  OR strpos(lower(CASE WHEN {_DESIGNED} THEN collections.cover ->> 'topic'
    ELSE coalesce(nullif(regexp_replace(btrim(collections.title), '\\s+', ' ', 'g'), ''),
      'Untitled collection') END), :needle) > 0
  OR (CASE WHEN {_DESIGNED} THEN EXISTS (
      SELECT 1 FROM jsonb_array_elements(CASE
        WHEN jsonb_typeof(collections.cover -> 'terms') = 'array'
        THEN collections.cover -> 'terms' ELSE '[]'::jsonb END) AS term
      WHERE jsonb_typeof(term) = 'string' AND strpos(lower(term #>> '{{}}'), :needle) > 0)
    ELSE EXISTS (
      SELECT 1 FROM (SELECT f.title FROM sources f WHERE f.collection_id = collections.id
        ORDER BY f.name COLLATE "C" LIMIT {cover.TERMS_MAX}) AS firsts
      WHERE strpos(lower(regexp_replace(firsts.title, '\\s+', ' ', 'g')), :needle) > 0)
    END))"""
)


async def search(s: AsyncSession, query: str) -> TextClause | None:
    """`SEARCH` for a search as a person typed it, ready to filter a query
    that joins `shares` and `collections`; None when it is empty and every
    share matches."""
    needle = query.strip().lower()
    if not needle:
        return None
    key = st.COVERS_KEY
    d = st.find(key)
    shared = await s.scalar(select(InstanceSetting.value).where(InstanceSetting.key == key))
    return SEARCH.bindparams(
        needle=needle,
        covers_key=key,
        covers_env=st.from_env(key),
        covers_user=d is None or d.scope == "user",
        covers_default=(shared or "").strip() or (d.default if d else ""),
    )


def _visible() -> Select[uuid.UUID]:
    """The ids of the shares of people whose account is on, with their
    collections joined, for a search to filter."""
    return (
        select(Share.id)
        .join(User, User.id == Share.owner_id)
        .join(Collection, Collection.id == Share.collection_id)
        .where(User.disabled_at.is_(None))
    )


@dataclass
class Feed:
    """One page of the feed, and where the next starts; None on the last."""

    cards: list[Card]
    next_offset: int | None


async def feed(
    s: AsyncSession,
    query: str,
    sort: Sort,
    limit: int = FEED_DEFAULT,
    offset: int = 0,
) -> Feed:
    """One page of the feed: the cards a search matches, newest share first,
    or most reused first with newest breaking the tie. Searched, sorted and
    cut to the page in the database; only the page's cards are then read in
    full, in a handful of queries whatever their number."""
    limit = max(1, min(limit, FEED_MAX))
    offset = max(0, offset)
    q = _visible()
    if (matched := await search(s, query)) is not None:
        q = q.where(matched)
    newest = (Share.updated_at.desc(), Share.id.desc())
    q = q.order_by(Share.reuses.desc(), *newest) if sort == "reused" else q.order_by(*newest)
    ids = list(await s.scalars(q.limit(limit + 1).offset(offset)))
    more = len(ids) > limit
    ids = ids[:limit]
    found = {c.id: c for c in await entries(s, ids)}
    return Feed([found[i] for i in ids if i in found], offset + limit if more else None)


async def _source_titles(s: AsyncSession, cids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """The first source titles of each collection, by name: what a cover not
    designed yet is drawn from."""
    out: dict[uuid.UUID, list[str]] = {cid: [] for cid in cids}
    if not cids:
        return out
    ranked = (
        select(
            Source.collection_id,
            Source.title,
            func.row_number()
            .over(partition_by=Source.collection_id, order_by=Source.name.collate("C"))
            .label("n"),
        )
        .where(Source.collection_id.in_(cids))
        .subquery()
    )
    rows = await s.execute(
        select(ranked.c.collection_id, ranked.c.title)
        .where(ranked.c.n <= cover.TERMS_MAX)
        .order_by(ranked.c.collection_id, ranked.c.n)
    )
    for cid, title in rows:
        out[cid].append(title)
    return out


async def entries(s: AsyncSession, share_ids: list[uuid.UUID]) -> list[Card]:
    """The cards of the shares asked for, of people whose account is on, in
    a handful of queries however many there are."""
    if not share_ids:
        return []
    rows = list(
        await s.execute(
            select(Share, User.display_name)
            .join(User, User.id == Share.owner_id)
            .where(User.disabled_at.is_(None), Share.id.in_(share_ids))
        )
    )
    cids = [sh.collection_id for sh, _ in rows]
    summaries = await collections.of_anyone(s, cids)
    held = await live(s, cids)
    titles = await _source_titles(s, cids)
    covers_on = await covers_on_of(s, {sh.owner_id for sh, _ in rows})
    out: list[Card] = []
    for sh, name in rows:
        summary = summaries.get(sh.collection_id)
        if summary is None:
            continue
        spec = covers.cover_spec(
            summary.collection, covers_on[sh.owner_id], titles[sh.collection_id]
        )
        out.append(card(sh, name, summary, included(sh.outputs, held[sh.collection_id]), spec))
    return out


@dataclass
class View:
    """One share as a visitor sees it."""

    card: Card
    # Empty when the share does not include them.
    sources: list[Source]
    # What it includes that is still there, in the share's order.
    outputs: list[Out]


async def view(s: AsyncSession, share_id: uuid.UUID) -> View:
    found = await entries(s, [share_id])
    if not found:
        raise gone()
    c = found[0]
    share = await s.get(Share, share_id)
    assert share is not None
    srcs = await shared_sources(s, share)
    held = (await live(s, [share.collection_id]))[share.collection_id]
    return View(c, srcs, included(share.outputs, held))


async def shared_sources(s: AsyncSession, share: Share) -> list[Source]:
    """The sources of a share, when it includes them, in the order they were
    added."""
    if not share.include_sources:
        return []
    # Without their text, which the share's page never shows and which can be
    # megabytes each.
    rows = await s.scalars(
        select(Source)
        .options(defer(Source.text, raiseload=True))
        .where(Source.collection_id == share.collection_id)
        .order_by(Source.created_at, Source.name)
    )
    return list(rows)


async def readable(s: AsyncSession, share_id: uuid.UUID) -> Share:
    """A share anyone signed in may read: there, and its sharer's account on."""
    share = await s.scalar(
        select(Share)
        .join(User, User.id == Share.owner_id)
        .where(Share.id == share_id, User.disabled_at.is_(None))
    )
    if share is None:
        raise gone()
    return share


async def output_of[T: (Session, MindMap, StudyNotes)](
    s: AsyncSession, share_id: uuid.UUID, model: type[T], oid: uuid.UUID
) -> T:
    """One output a share includes and that is still there, to read only.
    Anything it does not include is as not there, so an unshared output is
    never reachable through a share."""
    share = await readable(s, share_id)
    kind: KeyKind = "session" if model is Session else "mindmap" if model is MindMap else "notes"
    held = (await live(s, [share.collection_id]))[share.collection_id]
    if not any(o.id == oid and o.key.startswith(kind) for o in included(share.outputs, held)):
        raise not_found("That output")
    row = await s.get(model, oid)
    assert row is not None
    return row


async def source_of(s: AsyncSession, share_id: uuid.UUID, name: str) -> Source:
    """One source of a share that includes its sources, to read only."""
    share = await readable(s, share_id)
    src = None
    if share.include_sources:
        src = await s.scalar(
            select(Source).where(Source.collection_id == share.collection_id, Source.name == name)
        )
    if src is None:
        raise not_found("That source")
    return src


# ── what is inside the shares ─────────────────────────────────────────────────
#
# Discover's items: each output a share includes, on its own, to play or read
# without opening its collection first. The same rules as a share's page: only
# what the share includes, only what is there and ready, only shares of
# people whose account is on.

# The most items one page of the feed holds.
ITEMS_MAX = 60


@dataclass
class Item:
    """One output a share includes, as Discover lists it."""

    out: Out
    share_id: uuid.UUID
    collection_id: uuid.UUID
    # The collection's current title.
    collection_title: str
    cover_version: str
    # The sharer's display name; empty when they have none.
    shared_by: str
    owner_id: uuid.UUID
    # How many times its share was reused.
    reuses: int


@dataclass
class Items:
    """One page of items, and where the next starts; None when it is the last."""

    items: list[Item]
    next_offset: int | None


async def covers_on_of(s: AsyncSession, owners: set[uuid.UUID]) -> dict[uuid.UUID, bool]:
    """Whether each person's covers are designed, in two queries whatever their
    number, by the rule a single setting is read by."""
    return {o: st.is_on(v) for o, v in (await st.value_of_each(s, owners, st.COVERS_KEY)).items()}


def _included(kind: OutputKind | None) -> Any:
    """Every output a share includes that is there and ready, of the kind
    asked for (all four when none), with its share: each share's keys
    unnested and each looked up by its id in its own table, so the work
    grows with what is shared, not with everything everyone made."""
    k = func.unnest(Share.outputs).table_valued("key").render_derived("k")
    oid = func.split_part(k.c.key, ":", 2)
    keys = (
        select(
            Share.id.label("share_id"),
            Share.collection_id.label("collection_id"),
            func.split_part(k.c.key, ":", 1).label("prefix"),
            # A key whose id does not read as one names nothing, rather than
            # failing the whole page.
            case((func.pg_input_is_valid(oid, "uuid"), cast(oid, PgUUID(as_uuid=True)))).label(
                "oid"
            ),
            k.c.key,
        )
        .select_from(Share)
        .join(k, true())
        .subquery("keys")
    )
    parts: list[Any] = []
    for k in ("slides", "audio"):
        if kind in (None, k):
            parts.append(
                select(
                    literal(k, Text).label("kind"),
                    Session.id.label("id"),
                    Session.title.label("title"),
                    cast(func.jsonb_array_length(Session.slides), Integer).label("parts"),
                    Session.duration_ms.label("duration_ms"),
                    Session.created_at.label("created_at"),
                    keys.c.share_id,
                    keys.c.key,
                )
                .select_from(keys)
                .join(
                    Session,
                    and_(
                        Session.id == keys.c.oid,
                        Session.collection_id == keys.c.collection_id,
                    ),
                )
                .where(keys.c.prefix == "session", Session.kind == k, Session.state == "ready")
            )
    for k, model in (("mindmap", MindMap), ("notes", StudyNotes)):
        if kind in (None, k):
            parts.append(
                select(
                    literal(k, Text).label("kind"),
                    model.id.label("id"),
                    model.title.label("title"),
                    literal(0, Integer).label("parts"),
                    literal(0, BigInteger).label("duration_ms"),
                    model.created_at.label("created_at"),
                    keys.c.share_id,
                    keys.c.key,
                )
                .select_from(keys)
                .join(
                    model,
                    and_(model.id == keys.c.oid, model.collection_id == keys.c.collection_id),
                )
                .where(keys.c.prefix == k, model.state == "ready")
            )
    return (parts[0] if len(parts) == 1 else union_all(*parts)).subquery("o")


def cast_kind(k: str) -> OutputKind:
    assert k in ("slides", "audio", "mindmap", "notes")
    return k  # pyright: ignore[reportReturnType]


async def items(
    s: AsyncSession,
    kind: OutputKind | None,
    query: str,
    sort: Sort,
    limit: int = 24,
    offset: int = 0,
) -> Items:
    """One page of what the shares include, of one kind or all: only what a
    share includes and is there and ready, only shares of people whose account
    is on. A search matches an item by its own title, or by its share as the
    feed matches a card. Newest made first, or the most reused share's first
    with newest breaking the tie. Searched, sorted and cut to the page in the
    database, in a handful of queries whatever the page."""
    limit = max(1, min(limit, ITEMS_MAX))
    offset = max(0, offset)
    o = _included(kind)
    q = (
        select(
            o.c.kind,
            o.c.id,
            o.c.title,
            o.c.parts,
            o.c.duration_ms,
            o.c.created_at,
            Share.id,
            Share.owner_id,
            Share.reuses,
            Collection,
            User.display_name,
        )
        .select_from(o)
        .join(Share, Share.id == o.c.share_id)
        .join(Collection, Collection.id == Share.collection_id)
        .join(User, User.id == Share.owner_id)
        .where(User.disabled_at.is_(None))
    )
    if (matched := await search(s, query)) is not None:
        needle = query.strip().lower()
        # The shares a search matches, found once each rather than once per
        # item: its own `shares` and `collections`, not the outer query's.
        shared = _visible().where(matched).correlate(None)
        q = q.where(or_(func.strpos(func.lower(o.c.title), needle) > 0, Share.id.in_(shared)))
    newest = (o.c.created_at.desc(), o.c.id.desc())
    q = q.order_by(Share.reuses.desc(), *newest) if sort == "reused" else q.order_by(*newest)
    rows: list[Any] = list(await s.execute(q.limit(limit + 1).offset(offset)))
    more = len(rows) > limit
    rows = rows[:limit]

    cids = list({c.id for *_, c, _ in rows})
    names: dict[uuid.UUID, list[str]] = {cid: [] for cid in cids}
    if cids:
        for cid, ns in await s.execute(
            select(Source.collection_id, func.array_agg(Source.name))
            .where(Source.collection_id.in_(cids))
            .group_by(Source.collection_id)
        ):
            names[cid] = sorted(ns)
    on = await covers_on_of(s, {owner for *_, owner, _, _, _ in rows})
    out: list[Item] = []
    for k, id_, title, n, ms, at, sid, owner, reuses, c, name in rows:
        out.append(
            Item(
                out=Out(cast_kind(k), id_, title, at, n, ms),
                share_id=sid,
                collection_id=c.id,
                collection_title=c.title,
                cover_version=cover.current_version(
                    str(c.id), c.title, c.cover, on[owner], names[c.id]
                ),
                shared_by=name,
                owner_id=owner,
                reuses=reuses,
            )
        )
    return Items(out, offset + limit if more else None)


# ── the owner's side ──────────────────────────────────────────────────────────


async def of_collection(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> Share | None:
    """The share of one of the owner's collections, if it has one."""
    await collections.owned(s, owner, cid)
    return await s.scalar(select(Share).where(Share.collection_id == cid, Share.owner_id == owner))


async def put(
    s: AsyncSession,
    owner: uuid.UUID,
    cid: uuid.UUID,
    include_sources: bool,
    outputs: Sequence[str],
    note: str,
    allow_edits: bool = False,
) -> Share:
    """Share a collection, or change what its share includes. Under the
    collection's lock, so it cannot land behind its delete, and only while
    the collection is there. A read-only copy is not shared again."""
    await collections.refuse_read_only(s, await collections.lock(s, owner, cid))
    include_sources, keys, note = validate(include_sources, outputs, note, await holdings(s, cid))
    share = await s.scalar(select(Share).where(Share.collection_id == cid))
    now = datetime.now(UTC)
    if share is None:
        share = Share(owner_id=owner, collection_id=cid, created_at=now, reuses=0)
        s.add(share)
    share.include_sources = include_sources
    share.outputs = keys
    share.note = note
    share.allow_edits = allow_edits
    share.updated_at = now
    await s.flush()
    await s.refresh(share)
    return share


async def owned(s: AsyncSession, owner: uuid.UUID, share_id: uuid.UUID) -> Share:
    """A share its owner is changing: gone when there is none, refused when it
    is someone else's."""
    share = await s.get(Share, share_id)
    if share is None:
        raise gone()
    if share.owner_id != owner:
        raise not_yours()
    return share


async def change(
    s: AsyncSession,
    owner: uuid.UUID,
    share_id: uuid.UUID,
    include_sources: bool | None,
    outputs: Sequence[str] | None,
    note: str | None,
    allow_edits: bool | None = None,
) -> Share:
    """Change some of what a share includes; what is not given stays."""
    share = await owned(s, owner, share_id)
    return await put(
        s,
        owner,
        share.collection_id,
        share.include_sources if include_sources is None else include_sources,
        share.outputs if outputs is None else outputs,
        share.note if note is None else note,
        share.allow_edits if allow_edits is None else allow_edits,
    )


async def remove(s: AsyncSession, owner: uuid.UUID, share_id: uuid.UUID) -> None:
    """Stop sharing a collection. Copies people already made stay theirs."""
    share = await owned(s, owner, share_id)
    await collections.lock(s, owner, share.collection_id)
    await s.delete(share)


# ── reuse ─────────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    """What a reuse copies, resolved before anything is written."""

    sources: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    sessions: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    maps: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    notes: list[uuid.UUID] = field(default_factory=list[uuid.UUID])

    def empty(self) -> bool:
        return not (self.sources or self.sessions or self.maps or self.notes)

    def complete(self, held: Holdings) -> bool:
        """Whether the copy holds all the original does, so its cover,
        designed from that content, still describes it."""
        return (
            held.sources == len(self.sources)
            and len(held.sessions) == len(self.sessions)
            and len(held.maps) == len(self.maps)
            and len(held.notes) == len(self.notes)
        )


def plan_of(include_sources: bool, source_ids: list[uuid.UUID], inc: list[Out]) -> Plan:
    return Plan(
        sources=source_ids if include_sources else [],
        sessions=[o.id for o in inc if o.kind in ("slides", "audio")],
        maps=[o.id for o in inc if o.kind == "mindmap"],
        notes=[o.id for o in inc if o.kind == "notes"],
    )


def moved(value: Any, old: uuid.UUID, new: uuid.UUID) -> Any:
    """A deck's or audio overview's JSON as its copy's: every string that is
    the old id names the new one, and a path with the old id as a segment
    (`audio/<id>/<line>.wav`, `decks/<id>/…`) points into the copy's own
    directory. Anything else is kept as it was."""
    o, n = str(old), str(new)
    if isinstance(value, str):
        if value == o:
            return n
        parts = value.split("/")
        if len(parts) > 1 and o in parts:
            at = len(parts) - 1 - parts[::-1].index(o)
            parts[at] = n
            return "/".join(parts)
        return value
    if isinstance(value, list):
        return [moved(v, old, new) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: moved(v, old, new) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


# Where a deck's or audio overview's files are on the files volume, by its id.
OUTPUT_DIRS = ("decks", "audio", "video")


def _columns(row: DeclarativeBase, *skip: str) -> dict[str, Any]:
    """Every column of a row but the ones named and the generated ones, so a
    column added later is copied too."""
    table: Any = row.__table__
    return {
        c.key: getattr(row, c.key)
        for c in table.columns
        if c.key not in skip and c.computed is None and c.key != "id"
    }


def _copy_rows(
    model: type[Chunk | QaPair | MindMap | StudyNotes], match: ColumnElement[bool], **over: Any
) -> Insert:
    """`INSERT … SELECT` of the rows `match` finds, with the columns in `over`
    set to new values and fresh ids."""
    table: Any = model.__table__
    cols = [c for c in table.columns if c.key != "id" and c.computed is None]
    exprs = [literal(over[c.key], c.type).label(c.key) if c.key in over else c for c in cols]
    return insert(table).from_select([c.name for c in cols], select(*exprs).where(match))


def _id_map(pairs: dict[uuid.UUID, uuid.UUID]) -> Any:
    """Old ids and the new ids that replace them, as a table to join on."""
    return (
        values(column("old", PgUUID(as_uuid=True)), column("new", PgUUID(as_uuid=True)), name="m")
        .data(list(pairs.items()))
        .alias("m")
    )


def _copy_mapped(model: type[Source | Chunk | QaPair], ids: Any, on: str, **over: Any) -> Insert:
    """`INSERT … SELECT` of the rows whose `on` column holds one of the old
    ids in `ids`, each with `on` set to the new id it maps to and the columns
    in `over` set to new values. A row keyed on its own `id` takes the new id
    as its own; any other takes a fresh one. One statement however many rows."""
    table: Any = model.__table__
    cols = [c for c in table.columns if c.computed is None and (c.key != "id" or on == "id")]

    def expr(c: Any) -> Any:
        if c.key == on:
            return ids.c.new.label(c.key)
        if c.key in over:
            return literal(over[c.key], c.type).label(c.key)
        return c

    picked = select(*map(expr, cols)).join_from(table, ids, table.c[on] == ids.c.old)
    return insert(table).from_select([c.name for c in cols], picked)


def rewrite(old: Session, sid: uuid.UUID, owner: uuid.UUID, cid: uuid.UUID) -> Session:
    """An output's row as its copy: its own id, in the new collection, with
    every field that named the old id naming the new one. The copy cost
    nothing, is not pinned and has no failure."""
    cols = _columns(old, "owner_id", "collection_id")
    cols.update(
        slides=moved(old.slides, old.id, sid),
        deck_ref=moved(old.deck_ref, old.id, sid),
        audio=moved(old.audio, old.id, sid),
        speakers=moved(old.speakers, old.id, sid),
        # Only finished videos come along: a render in flight belongs to the
        # original's job, which never writes to the copy.
        video=moved(
            {k: v for k, v in (old.video or {}).items() if v.get("state") == "ready"} or None,
            old.id,
            sid,
        ),
        failure=None,
        pinned=False,
        spent_usd=Decimal(0),
        spent_known=True,
    )
    return Session(id=sid, owner_id=owner, collection_id=cid, **cols)


def _cleanup_on_rollback(s: AsyncSession, paths: list[str]) -> None:
    """Files a copy writes go again when its rows do not land, whether the
    copy failed half way or the commit did."""

    def rolled_back(_: Any) -> None:
        for rel in paths:
            storage.remove_tree(rel)

    event.listen(s.sync_session, "after_rollback", rolled_back, once=True)


async def reuse(s: AsyncSession, me: uuid.UUID, share_id: uuid.UUID) -> Collection:
    """Copy a shared collection into a new one of `me`'s, under the original's
    lock, so the original cannot be deleted half way through. The new one is
    not visible to anyone until the transaction commits, whole. A failure
    leaves nothing of the copy, rows or files."""
    first = await readable(s, share_id)
    try:
        original = await collections.lock(s, first.owner_id, first.collection_id)
    except Problem as e:
        raise gone() from e
    # Again under the lock: it may have been unshared meanwhile.
    share = await s.scalar(select(Share).where(Share.id == share_id).with_for_update())
    if share is None:
        raise gone()
    cid = share.collection_id
    held = await holdings(s, cid)
    source_ids = list(
        await s.scalars(
            select(Source.id).where(Source.collection_id == cid).order_by(Source.created_at)
        )
    )
    plan = plan_of(
        share.include_sources, source_ids, included(share.outputs, (await live(s, [cid]))[cid])
    )
    if plan.empty():
        raise Problem(
            409,
            "Nothing this share included is still there, so there is nothing to reuse. "
            "Look in Discover for another one.",
        )

    title = " ".join(original.title.split())
    copy = Collection(
        owner_id=me,
        title=title,
        # A copy of a collection still waiting for its name is named like any
        # other.
        title_auto=not title,
        reused_from=share.id,
        # Fixed now: a later change to the share leaves this copy as it is.
        read_only=not share.allow_edits,
        cover=original.cover,
        cover_version=original.cover_version,
    )
    s.add(copy)
    await s.flush()
    await s.refresh(copy)
    sids = {old: uuid.uuid7() for old in plan.sessions}
    _cleanup_on_rollback(
        s,
        [f"uploads/{copy.id}", *(f"{d}/{sid}" for sid in sids.values() for d in OUTPUT_DIRS)],
    )

    # Every source, its passages and their vectors in three statements,
    # whatever their number: the copy is searched and asked like the
    # original without a model call. New ids are chosen here, so the
    # passages can point at their copied source in the same statements.
    srcs = {old: uuid.uuid7() for old in plan.sources}
    if srcs:
        ids = _id_map(srcs)
        mine = {"owner_id": me, "collection_id": copy.id}
        await s.execute(_copy_mapped(Source, ids, "id", file_path=None, **mine))
        for model in (Chunk, QaPair):
            await s.execute(_copy_mapped(model, ids, "source_id", **mine))
        # The original uploads, for the few sources that have one.
        files = await s.execute(
            select(Source.id, Source.file_path).where(
                Source.id.in_(list(srcs)), Source.file_path.is_not(None)
            )
        )
        for old_id, path in files:
            if path and storage.exists(path):
                new = srcs[old_id]
                kept = storage.copy_new(
                    path, f"uploads/{copy.id}/{new}{PurePosixPath(path).suffix}"
                )
                await s.execute(update(Source).where(Source.id == new).values(file_path=kept))

    olds = {o.id: o for o in await s.scalars(select(Session).where(Session.id.in_(list(sids))))}
    for old_id, sid in sids.items():
        old = olds[old_id]
        for d in OUTPUT_DIRS:
            storage.copy_tree(f"{d}/{old_id}", f"{d}/{sid}")
        s.add(rewrite(old, sid, me, copy.id))

    for model, ids_ in ((MindMap, plan.maps), (StudyNotes, plan.notes)):
        if ids_:
            await s.execute(
                _copy_rows(
                    model, model.id.in_(ids_), owner_id=me, collection_id=copy.id, job_id=None
                )
            )
    await s.flush()

    # The cover is kept as current only when the copy holds everything the
    # original did; otherwise it is designed again from what the copy holds.
    if plan.complete(held) and copy.cover is not None:
        copy.cover_from = covers.content_key((await covers.gather(s, me, [copy.id]))[copy.id])
    await s.execute(update(Share).where(Share.id == share.id).values(reuses=Share.reuses + 1))
    return copy
