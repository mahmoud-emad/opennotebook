"""Collections: start one, list them, read one with its outputs, rename, pin,
delete."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from opennotebook import storage
from opennotebook.api.deps import Db, Me
from opennotebook.api.sessions import SessionSummary, summaries_of
from opennotebook.cover import Theme
from opennotebook.domain import collections, covers, refresh

router = APIRouter(prefix="/api/collections", tags=["collections"])


class CollectionSummary(BaseModel):
    id: uuid.UUID
    title: str
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

    @classmethod
    def of(cls, s: collections.Summary) -> CollectionSummary:
        c = s.collection
        return cls(
            id=c.id,
            title=c.title,
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


@router.get("")
async def list_collections(s: Db, me: Me) -> list[CollectionSummary]:
    """Every collection, most recently updated first, with counts of what it holds."""
    listed = await collections.list_all(s, me.id)
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
    await covers.redraw(me.id, cid, force=True)
    # The design was written in its own transaction; read it fresh.
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
    return HTMLResponse(html, headers={"Cache-Control": cache})
