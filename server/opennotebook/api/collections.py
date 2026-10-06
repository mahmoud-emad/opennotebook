"""Collections: start one, list them, read one with its outputs, rename, pin,
delete, and follow one as it changes."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from opennotebook import storage
from opennotebook.api import paging
from opennotebook.api.deps import SANDBOXED, Db, Me
from opennotebook.api.mindmaps import maps_of
from opennotebook.api.notes import notes_of
from opennotebook.api.sessions import SessionSummary, summaries_and_jobs, summaries_of
from opennotebook.api.sources import SourceOut, listed
from opennotebook.auth import current_user
from opennotebook.build import phases
from opennotebook.cover import Theme
from opennotebook.db.models import Job, Source
from opennotebook.db.session import release, sessionmaker
from opennotebook.domain import collections, covers, refresh
from opennotebook.errors import Problem
from opennotebook.jobs.events import JOB, REREAD_SECONDS, hub

router = APIRouter(prefix="/api/collections", tags=["collections"])


class CollectionSummary(BaseModel):
    id: uuid.UUID
    title: str
    display_title: str = Field(
        description="Its name as it is shown: the title, or `Untitled collection` while it has none"
    )
    title_auto: bool = Field(description="The studio named it; a person's title is never replaced")
    pinned: bool
    created_at: datetime
    updated_at: datetime = Field(description="The last time a source or an output changed")
    cover_version: str = Field(
        description="The cover drawn now: `f-…` until one is designed, `g-…` after. "
        "Load the cover with it as `v`, so a new one is fetched when it changes"
    )
    sources: int
    decks: int = Field(description="Narrated slide decks, any state")
    audios: int = Field(description="Audio overviews, any state")
    maps: int
    notes: int
    preparing: int = Field(description="Decks and audio overviews still being made")
    failed: int
    reused_from: uuid.UUID | None = Field(
        description="The share this collection was copied from by a reuse; null when none. "
        "The share may since have been removed"
    )
    shared: bool = Field(
        description="It has a share, so everyone on the studio sees it in Discover"
    )
    reuses: int = Field(description="How many times its share was reused; 0 when not shared")
    read_only: bool = Field(
        description="A copy whose share did not allow edits: it can be read, asked, pinned "
        "and deleted, but nothing in it changes and it cannot be shared"
    )
    reused_from_title: str | None = Field(
        description="The name of the collection it was reused from, as it is shown now; "
        "null when it was not reused or that share is gone"
    )
    busy: bool = Field(
        description="Something in it is still being made: an output, its name or its cover. "
        "Follow /api/collections/{cid}/events while it is"
    )
    auto_named: bool = Field(
        description="The studio names it from its sources: automatic naming is on in "
        "Settings and nobody has given it a name"
    )
    name_note: str | None = Field(
        description="What the studio is doing with its name, in words: `Naming it from its "
        "sources…` or `Named from its sources`; null when the name is a person's"
    )

    @classmethod
    def of(cls, s: collections.Summary) -> CollectionSummary:
        c = s.collection
        note = None
        if s.auto_named and c.title.strip():
            note = "Named from its sources"
        elif s.auto_named and s.sources > 0:
            note = "Naming it from its sources…"
        return cls(
            id=c.id,
            title=c.title,
            display_title=collections.display_title(c.title),
            title_auto=c.title_auto,
            pinned=c.pinned,
            created_at=c.created_at,
            updated_at=c.updated_at,
            cover_version=s.cover_version,
            sources=s.sources,
            decks=s.decks,
            audios=s.audios,
            maps=s.maps,
            notes=s.notes,
            preparing=s.preparing,
            failed=s.failed,
            reused_from=c.reused_from,
            shared=s.shared,
            reuses=s.reuses,
            read_only=c.read_only,
            reused_from_title=None
            if s.reused_title is None
            else collections.display_title(s.reused_title),
            busy=s.busy,
            auto_named=s.auto_named,
            name_note=note,
        )


class CollectionDetail(BaseModel):
    collection: CollectionSummary
    outputs: list[SessionSummary] = Field(description="Decks and audio overviews, newest first")


class CollectionCreate(BaseModel):
    title: str = Field(
        default="",
        max_length=200,
        description="What the person called it; empty and the studio names it from its sources",
    )


class CollectionPatch(BaseModel):
    title: str | None = Field(
        default=None, max_length=200, description="Empty hands the naming back to the studio"
    )
    pinned: bool | None = None


# How many collections a page of the list holds unless asked for fewer, and
# at most.
LIST_DEFAULT = 200
LIST_MAX = 500


@router.get("")
async def list_collections(
    s: Db,
    me: Me,
    response: Response,
    limit: Annotated[int, paging.limit(LIST_DEFAULT, LIST_MAX, "collections")] = LIST_DEFAULT,
    offset: paging.Offset = 0,
) -> list[CollectionSummary]:
    """Your collections, most recently updated first, with counts of what
    each holds, a page at a time: `X-Next-Offset` says where the next page
    starts when there is one."""
    listed = paging.cut(
        await collections.list_all(s, me.id, limit + 1, offset), limit, offset, response
    )
    # A collection whose cover is older than what it holds (an output that
    # finished while nothing was listening) gets one designed in the
    # background. This list shows the cover it has.
    await refresh.enqueue_covers(s, me.id, await covers.stale(s, me.id, listed))
    return [CollectionSummary.of(c) for c in listed]


@router.post("", status_code=201)
async def create_collection(body: CollectionCreate, s: Db, me: Me) -> CollectionSummary:
    """Start a collection: an empty set of sources to add to and build outputs from."""
    c = await collections.create(s, me.id, body.title)
    return CollectionSummary.of(await collections.summary(s, me.id, c.id))


@router.get("/{cid}")
async def get_collection(cid: uuid.UUID, s: Db, me: Me) -> CollectionDetail:
    """One collection, with its decks and audio overviews."""
    summary = await collections.summary(s, me.id, cid)
    # As the list does: opening the collection is enough to bring its cover
    # up to date.
    await refresh.enqueue_covers(s, me.id, await covers.stale(s, me.id, [summary]))
    return CollectionDetail(
        collection=CollectionSummary.of(summary), outputs=await summaries_of(s, me.id, cid)
    )


@router.patch("/{cid}")
async def update_collection(
    cid: uuid.UUID, body: CollectionPatch, s: Db, me: Me
) -> CollectionSummary:
    """Rename or pin a collection. A read-only copy can be pinned, not renamed."""
    if body.title is not None:
        await collections.retitle(s, me.id, cid, body.title)
        if not body.title.strip():
            # Handed back to the studio: it names it again from the sources.
            await refresh.schedule(s, me.id, cid)
    if body.pinned is not None:
        await collections.pin(s, me.id, cid, body.pinned)
    return CollectionSummary.of(await collections.summary(s, me.id, cid))


@router.delete("/{cid}", status_code=204)
async def delete_collection(cid: uuid.UUID, s: Db, me: Me, after: BackgroundTasks) -> None:
    """Delete a collection with its sources and everything made from them."""
    await collections.remove(s, me.id, cid)
    # Its files go once the rows are gone for good.
    after.add_task(storage.remove_tree, f"uploads/{cid}")


@router.post("/{cid}/cover")
async def refresh_cover(cid: uuid.UUID, s: Db, me: Me) -> CollectionSummary:
    """Design the collection's cover again, from what it holds now, and answer
    once it is drawn. With covers off in Settings no model is asked and the
    cover stays the one drawn from the title. If the model fails, the cover
    the collection had is kept and the answer says why."""
    before = await collections.summary(s, me.id, cid)
    await collections.refuse_read_only(s, before.collection)
    # The model is asked with no transaction open; the design is written in
    # a short one of its own, under the collection's lock.
    await release(s)
    await covers.redraw(me.id, cid, force=True)
    # Read fresh, after the design was written.
    await s.refresh(before.collection)
    return CollectionSummary.of(await collections.summary(s, me.id, cid))


@router.get("/{cid}/cover", response_class=HTMLResponse)
async def read_cover(
    cid: uuid.UUID,
    s: Db,
    me: Me,
    v: Annotated[str, Query(description="The `cover_version` the page was listed with")] = "",
    theme: Annotated[str, Query(description="`light`, else the dark default")] = "dark",
) -> HTMLResponse:
    """The collection's cover: a self-contained HTML page (an SVG, no script,
    nothing fetched) for a sandboxed iframe. Cached for good when `v` is the
    version drawn now, so a new version is a new URL."""
    html, version = await covers.page(s, me.id, cid, Theme.parse(theme))
    cache = "private, max-age=31536000, immutable" if v == version else "no-cache"
    return HTMLResponse(html, headers={"Cache-Control": cache, **SANDBOXED})


# ── following one ────────────────────────────────────────────────────────────

# How often a busy collection is read again with nothing announced: the end of
# its naming or cover job is not announced, only what it wrote.
BUSY_REREAD_SECONDS = 3.0

Event = tuple[str, Any]


async def _read(owner: uuid.UUID, cid: uuid.UUID) -> dict[str, Any] | None:
    """The collection as the page shows it now: its summary, its decks and
    audio overviews, the progress of those being made, its sources, and its
    mind maps and study notes with the jobs of those being made. None once
    it is gone."""
    async with sessionmaker()() as s, s.begin():
        try:
            summary = await collections.summary(s, owner, cid)
        except Problem:
            return None
        outputs, making = await summaries_and_jobs(s, owner, cid)
        for job in making.values():
            # Its job's reports wake this collection's streams.
            hub.follow_job(job.id, cid)
        progress = _progress(list(making.values()))
        rows = await s.scalars(
            listed()
            .where(Source.collection_id == cid, Source.owner_id == owner)
            .order_by(Source.created_at)
        )
        maps = await maps_of(s, owner, cid)
        notes = await notes_of(s, owner, cid)
        made = [x.job_id for x in (*maps, *notes) if x.state == "making" and x.job_id is not None]
        for job_id in made:
            hub.follow_job(job_id, cid)
        return {
            "collection": CollectionSummary.of(summary).model_dump(mode="json"),
            "outputs": [o.model_dump(mode="json") for o in outputs],
            "sources": [SourceOut.of(r).model_dump(mode="json") for r in rows],
            "progress": progress,
            "jobs": [j.id for j in making.values()],
            "mindmaps": [m.model_dump(mode="json") for m in maps],
            "notes": [n.model_dump(mode="json") for n in notes],
            "made": made,
        }


def _progress(made: list[Job]) -> dict[str, dict[str, Any]]:
    """How far each output being made is, by its id, from its job; only jobs
    that report their steps."""
    return {
        str(j.session_id): {
            "session_id": str(j.session_id),
            "step": j.step,
            "label": phases.label(j.step),
            "steps_done": j.steps_done,
            "steps_total": j.steps_total,
        }
        for j in made
        if j.steps_total and j.session_id is not None
    }


async def _progress_of(owner: uuid.UUID, job_ids: list[uuid.UUID]) -> dict[str, dict[str, Any]]:
    """`_progress` read again from the jobs alone: what a job's report can
    have changed, without reading the collection again."""
    async with sessionmaker()() as s:
        made = await s.scalars(select(Job).where(Job.id.in_(job_ids), Job.owner_id == owner))
        return _progress(list(made))


# Why a map or notes job ended without a reason of its own.
STOPPED = "It stopped before it finished. Try again."


async def _ended(owner: uuid.UUID, job_ids: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """The jobs of `job_ids` that have ended, each with why it failed or
    stopped, or None when it made what it was making."""
    async with sessionmaker()() as s:
        rows = await s.execute(
            select(Job.id, Job.status, Job.error).where(
                Job.id.in_(job_ids),
                Job.owner_id == owner,
                Job.status.in_(("done", "failed", "cancelled")),
            )
        )
        return {i: None if st == "done" else (err or STOPPED) for i, st, err in rows}


async def events_of(owner: uuid.UUID, cid: uuid.UUID) -> AsyncGenerator[Event | None]:
    """A collection's events as they happen; None is a keep-alive. Each part
    is sent when the stream starts and again whenever it changes, so a page
    that connects late misses nothing. Ends with `gone` when the collection
    is deleted."""
    last: dict[str, Any] = {}
    sent: dict[str, Any] = {}
    # The jobs of the outputs being made, as the last whole read found them.
    jobs: list[uuid.UUID] = []
    # The jobs of the maps and notes being made that have not been told
    # ended yet.
    made: set[uuid.UUID] = set()
    async with hub.subscribe(cid) as woken:
        while True:
            why = woken.clear()
            # Asked before the lists are read: a map or notes are written
            # before their job ends, so a page hears of the list first and
            # of the end after.
            done = await _ended(owner, made) if made else {}
            if why == {JOB} and jobs and not done:
                # Only a job reported: its progress is all that can have
                # moved. Its outcome comes with a change to the collection.
                progress = await _progress_of(owner, jobs)
            else:
                now = await _read(owner, cid)
                if now is None:
                    yield ("gone", {"collection_id": str(cid)})
                    return
                for part in ("collection", "outputs", "sources", "mindmaps", "notes"):
                    if last.get(part) != now[part]:
                        yield (part, now[part])
                        last[part] = now[part]
                progress, jobs = now["progress"], now["jobs"]
                made.update(now["made"])
            for sid, p in progress.items():
                if sent.get(sid) != p:
                    yield ("progress", p)
                    sent[sid] = p
            for job_id, error in done.items():
                made.discard(job_id)
                yield ("ended", {"job_id": str(job_id), "error": error})
            wait = BUSY_REREAD_SECONDS if last["collection"]["busy"] else REREAD_SECONDS
            try:
                async with asyncio.timeout(wait):
                    await woken.wait()
            except TimeoutError:
                yield None


async def _frames(owner: uuid.UUID, cid: uuid.UUID) -> AsyncIterator[str]:
    async for e in events_of(owner, cid):
        # A comment frame when nothing changed: proxies close a silent
        # connection.
        yield ": keepalive\n\n" if e is None else f"event: {e[0]}\ndata: {json.dumps(e[1])}\n\n"


@router.get(
    "/{cid}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}},
)
async def collection_events(cid: uuid.UUID, request: Request) -> StreamingResponse:
    """Server-sent events while a collection is open: `collection` (its
    summary, as the list has it), `outputs` (its decks and audio overviews),
    `progress` (`session_id`, `step`, `label` (the step as a person reads
    it), `steps_done`, `steps_total` of one being made), `sources` (its
    sources), `mindmaps` and `notes` (as their lists have them, those being
    made included), each when the stream starts and again when it changes;
    `ended` (`job_id`, and `error`: why it was not made, or null) once a map
    or notes being made is done; and `gone` once it is deleted. A page
    follows this rather than reading the collection again on a timer."""
    # Signed in and checked on a session of its own, closed before the
    # stream starts: a stream lasts as long as the page, and holds no
    # transaction.
    async with sessionmaker()() as s, s.begin():
        me = await current_user(request, s)
        await collections.owned(s, me.id, cid)
    return StreamingResponse(
        _frames(me.id, cid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
