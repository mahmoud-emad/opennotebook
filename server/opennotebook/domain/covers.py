"""When a collection's cover is designed, and what it is designed from.

Ported from the cover parts of `opennotebook_server/src/collection.rs`
(`content_key`, `wants_design`, `stale_covers`, `redraw`, `cover_refresh`,
`cover_spec`) and `cover::design`. Drawing is `opennotebook.cover`. Plain
async functions with their own database sessions, so they can move into a
background job as they are.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import null, select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import cover, jobs
from opennotebook.ai import ledger
from opennotebook.ai.client import ai
from opennotebook.ai.errors import AiError
from opennotebook.cover import CoverSpec, NotACover
from opennotebook.db.models import Collection, MindMap, Session, Source, StudyNotes
from opennotebook.db.session import sessionmaker
from opennotebook.domain import collections, naming
from opennotebook.domain import settings as st
from opennotebook.errors import Problem

log = logging.getLogger(__name__)


@dataclass
class Made:
    """A ready output, a map or a set of notes, as a cover reads it."""

    id: str
    kind: str
    title: str
    created_at: datetime
    parts: list[str] = field(default_factory=list[str])


@dataclass
class Held:
    """What one collection holds that its cover is designed from."""

    # Source names, sorted, and their titles in the same order.
    names: list[str] = field(default_factory=list[str])
    titles: list[str] = field(default_factory=list[str])
    # Ready decks and audio overviews, then mind maps, then notes, each
    # newest first.
    made: list[Made] = field(default_factory=list[Made])


def _newest_first(items: list[Made]) -> list[Made]:
    return sorted(items, key=lambda m: (m.created_at, m.id), reverse=True)


def _names_of(v: Any, key: str) -> list[str]:
    """The `key` of each dict in a JSON list: a map's main topics, the
    headings of a set of notes, the titles of a deck's slides."""
    if not isinstance(v, list):
        return []
    return [str(x.get(key) or "") for x in v if isinstance(x, dict)]


async def gather(
    s: AsyncSession, owner: uuid.UUID, cids: list[uuid.UUID], *, parts: bool = False
) -> dict[uuid.UUID, Held]:
    """What each of `cids` holds, in four queries whatever their number.

    Without `parts`, only what `content_key` hashes is read: each output's id
    and title, never a deck's lines, a map's tree or a set of notes. With
    them, each output's part names too (a deck's slide titles, a map's main
    topics, the headings of a set of notes), for a design's digest."""
    out: dict[uuid.UUID, Held] = {cid: Held() for cid in cids}
    sources = await s.execute(
        select(Source.collection_id, Source.name, Source.title).where(
            Source.owner_id == owner, Source.collection_id.in_(cids)
        )
    )
    for cid, name, title in sorted(sources, key=lambda r: r[1]):
        out[cid].names.append(name)
        out[cid].titles.append(title)
    made: dict[uuid.UUID, list[list[Made]]] = defaultdict(lambda: [[], [], []])
    sessions = await s.execute(
        select(
            Session.collection_id,
            Session.id,
            Session.kind,
            Session.title,
            Session.created_at,
            Session.slides if parts else null(),
        ).where(
            Session.owner_id == owner, Session.collection_id.in_(cids), Session.state == "ready"
        )
    )
    for cid, id_, k, title, at, slides in sessions:
        kind = "Audio overview" if k == "audio" else "Narrated slides"
        names = [t.strip() for t in _names_of(slides, "title") if t.strip()]
        made[cid][0].append(Made(str(id_), kind, title, at, names))
    maps = await s.execute(
        select(
            MindMap.collection_id,
            MindMap.id,
            MindMap.title,
            MindMap.created_at,
            MindMap.root if parts else null(),
        ).where(
            MindMap.owner_id == owner, MindMap.collection_id.in_(cids), MindMap.state == "ready"
        )
    )
    for cid, id_, title, at, root in maps:
        children = _names_of((root or {}).get("children"), "name")
        made[cid][1].append(Made(str(id_), "Mind map", title, at, children))
    notes = await s.execute(
        select(
            StudyNotes.collection_id,
            StudyNotes.id,
            StudyNotes.title,
            StudyNotes.created_at,
            StudyNotes.body if parts else null(),
        ).where(
            StudyNotes.owner_id == owner,
            StudyNotes.collection_id.in_(cids),
            StudyNotes.state == "ready",
        )
    )
    for cid, id_, title, at, body in notes:
        headings = _names_of((body or {}).get("ideas"), "heading")
        made[cid][2].append(Made(str(id_), "Study notes", title, at, headings))
    for cid, groups in made.items():
        out[cid].made = [m for group in groups for m in _newest_first(group)]
    return out


def content_key(held: Held) -> str:
    """What a cover is designed from, as a short hash: the source names and
    each ready output's id and title. A touch that changed none of them, a
    rename or a pin, is not a reason to design it again."""
    outs = sorted(f"{m.id}\x1f{m.title}" for m in held.made)
    return f"{cover.hash([*held.names, '--', *outs]):016x}"


def wants_design(covers_on: bool, force: bool, holds: bool, drawn_from: str, key: str) -> bool:
    """Whether a cover should be designed now: covers on, something to read,
    and either asked for or made from content that has since changed."""
    return covers_on and holds and (force or drawn_from != key)


def holds(s: collections.Summary) -> bool:
    """Whether the collection holds anything a cover can be designed from."""
    return bool(s.sources or s.maps or s.notes) or s.decks + s.audios > s.preparing + s.failed


def stale_covers(
    covers_on: bool, summaries: list[collections.Summary], held: dict[uuid.UUID, Held]
) -> list[uuid.UUID]:
    """The collections whose covers are out of date, in the order given (most
    recently updated first). None at all with covers off."""
    return [
        x.collection.id
        for x in summaries
        if wants_design(
            covers_on,
            False,
            holds(x),
            x.collection.cover_from,
            content_key(held.get(x.collection.id, Held())),
        )
    ]


async def stale(
    s: AsyncSession, owner: uuid.UUID, summaries: list[collections.Summary]
) -> list[uuid.UUID]:
    """`stale_covers` for summaries just listed, reading what they hold."""
    if not summaries or not st.is_on(await st.value(s, owner, st.COVERS_KEY)):
        return []
    held = await gather(s, owner, [x.collection.id for x in summaries])
    return stale_covers(True, summaries, held)


def cover_spec(c: Collection, covers_on: bool, source_titles: list[str]) -> CoverSpec:
    """The spec a cover is drawn from now: the designed one while covers are
    on, else the one drawn from the cid, title and first sources' titles."""
    if covers_on and (spec := CoverSpec.from_json(c.cover)):
        return spec
    return cover.fallback(str(c.id), c.title, source_titles[: cover.TERMS_MAX])


async def page(
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, theme: cover.Theme
) -> tuple[str, str]:
    """A collection's cover page in `theme`, with the version it is."""
    summary = await collections.summary(s, owner, cid)
    covers_on = st.is_on(await st.value(s, owner, st.COVERS_KEY))
    titles = await s.scalars(
        select(Source.title)
        .where(Source.collection_id == cid, Source.owner_id == owner)
        .order_by(Source.name.collate("C"))
        .limit(cover.TERMS_MAX)
    )
    spec = cover_spec(summary.collection, covers_on, list(titles))
    return cover.render(str(cid), spec, theme), summary.cover_version


# ── designing one ─────────────────────────────────────────────────────────────


async def design(cid: uuid.UUID, digest: naming.Digest, model: str, language: str) -> CoverSpec:
    """One model call: the digest in, a validated cover out."""
    done = await ai().complete(
        model,
        [
            {"role": "system", "content": cover.system_prompt(language)},
            {"role": "user", "content": digest.cover_prompt()},
        ],
        max_tokens=300,
    )
    return cover.validate(str(cid), done.text)


def _problem(e: AiError | NotACover | TimeoutError) -> Problem:
    """Why a cover asked for was not designed, for the person who asked."""
    if isinstance(e, AiError):
        return Problem(503 if e.retryable else 502, e.sentence)
    if isinstance(e, NotACover):
        return Problem(
            502,
            "The AI model's answer could not be drawn as a cover, so the cover was kept. "
            "Try again, or pick another chat model in Settings › Models.",
        )
    return Problem(
        503,
        "The AI model took too long to design the cover, so it was kept. Try again in a minute.",
    )


async def redraw(owner: uuid.UUID, cid: uuid.UUID, *, force: bool) -> None:
    """Design a collection's cover from what it holds, unless nothing changed
    since the last design (`force` designs it anyway).

    The model is asked outside the lock; the row is written under it, re-read
    first, and only while the collection is there: a collection deleted while
    the model thought stays deleted. A failed design keeps the cover the
    collection had and still records what it was tried on, so a model that
    is down is asked again when the content changes, not on every list.
    Asked for (`force`), a failure is raised as a `Problem` once that is
    written; in the background it goes to the log.
    """
    async with sessionmaker()() as s:
        if not st.is_on(await st.value(s, owner, st.COVERS_KEY)):
            return
        try:
            summary = await collections.summary(s, owner, cid)
        except Problem:
            return
        held = (await gather(s, owner, [cid], parts=True))[cid]
        key = content_key(held)
        if not wants_design(True, force, holds(summary), summary.collection.cover_from, key):
            return
        digest = naming.Digest(
            title=summary.collection.title.strip(),
            sources=naming.openings(await naming.sources_of(s, cid)),
            made=[naming.Part(m.kind, m.title, m.parts) for m in held.made],
        )
        model, language = await naming.model_settings(s, owner)
    designed: CoverSpec | None = None
    failure: AiError | NotACover | TimeoutError | None = None
    try:
        async with (
            asyncio.timeout(cover.DESIGN_TIMEOUT),
            ledger.spending(owner, "cover", collection_id=cid),
        ):
            designed = await design(cid, digest, model, language)
    except (AiError, NotACover, TimeoutError) as e:
        log.info("collection %s: cover kept: %s", cid, e)
        failure = e
    async with sessionmaker()() as s, s.begin():
        c = await s.scalar(
            select(Collection)
            .where(Collection.id == cid, Collection.owner_id == owner)
            .with_for_update()
        )
        if c is None:
            return
        if designed is not None:
            c.cover = designed.to_json()
            c.cover_version = cover.version(designed)
            await jobs.announce(s, cid)
        c.cover_from = key
    if failure is not None and force:
        raise _problem(failure)
