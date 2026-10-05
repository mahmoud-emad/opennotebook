"""Sharing and Discover: share a collection with everyone on the studio,
browse what people shared, read a share, and reuse one as a copy of your own.

A share is read through these routes only. The collection's own routes stay
its owner's, so nothing a share leaves out is reachable by anyone else.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, computed_field
from sqlalchemy import select

from opennotebook.api.collections import CollectionSummary
from opennotebook.api.deps import SANDBOXED, Db, Me
from opennotebook.api.mindmaps import MindMapOut
from opennotebook.api.notes import NotesOut
from opennotebook.api.sessions import SessionDetail
from opennotebook.api.sources import SourceOut, SourceText
from opennotebook.cover import Theme
from opennotebook.db.models import MindMap, Session, Share, StudyNotes
from opennotebook.domain import collections, covers, refresh, shares

router = APIRouter(prefix="/api", tags=["shares"])

OUTPUT_KEY = "`session:<id>` (a deck or an audio overview), `mindmap:<id>` or `notes:<id>`"

# What an output is called while it has no name, by its kind.
UNTITLED = {
    "slides": "Untitled narrated slides",
    "audio": "Untitled audio overview",
    "mindmap": "Untitled mind map",
    "notes": "Untitled study notes",
}


def shown(title: str, kind: str) -> str:
    """An output's name as it is shown: its title, or what it is while it
    has none."""
    return " ".join(title.split()) or UNTITLED[kind]


class ShareOut(BaseModel):
    """A share as its owner sets it."""

    id: uuid.UUID
    collection_id: uuid.UUID
    include_sources: bool
    outputs: list[str] = Field(
        description=f"Output keys, {OUTPUT_KEY}, in the order the owner picked them. "
        "One deleted since stays here and is skipped wherever the share is read"
    )
    note: str = Field(description="The owner's note, at most 280 characters; empty when none")
    reuses: int = Field(description="How many times it was reused")
    allow_edits: bool = Field(
        description="Copies made from it are their reuser's to change and share again; "
        "when false each copy is read-only. A change applies to copies made after it"
    )
    created_at: datetime
    updated_at: datetime = Field(description="The last time it was set: the feed's newest order")

    @classmethod
    def of(cls, s: Share) -> ShareOut:
        return cls.model_validate(s, from_attributes=True)


class ShareCard(BaseModel):
    """A share as the feed shows it. Counts are of what it includes and is
    still there."""

    id: uuid.UUID
    collection_id: uuid.UUID
    mine: bool = Field(description="The person asking shared it, so they can edit or stop it")
    shared_by: str = Field(description="The sharer's display name; empty when they have none")
    title: str = Field(description="The collection's current title")
    note: str
    cover_version: str = Field(
        description="Load the cover from /api/shares/{id}/cover with it as `v`"
    )
    terms: list[str] = Field(description="The cover's terms")
    sources: int = Field(description="0 when sources are not included")
    decks: int
    audios: int
    maps: int
    notes: int
    reuses: int
    allow_edits: bool = Field(
        description="A copy made now is the reuser's to change and share again; "
        "else it is read-only"
    )
    created_at: datetime
    updated_at: datetime

    @computed_field(
        description="The collection's name as it is shown: its title, or `Untitled collection`"
    )
    @property
    def display_title(self) -> str:
        return collections.display_title(self.title)

    @classmethod
    def of(cls, c: shares.Card, me: uuid.UUID) -> ShareCard:
        return cls(
            id=c.id,
            collection_id=c.collection_id,
            mine=c.owner_id == me,
            shared_by=c.shared_by,
            title=c.title,
            note=c.note,
            cover_version=c.cover_version,
            terms=c.terms,
            sources=c.sources,
            decks=c.decks,
            audios=c.audios,
            maps=c.maps,
            notes=c.notes,
            reuses=c.reuses,
            allow_edits=c.allow_edits,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )


class SharedOutput(BaseModel):
    key: str = Field(description="The output key, as in the share's outputs")
    kind: Literal["slides", "audio", "mindmap", "notes"]
    id: uuid.UUID = Field(
        description="Read it at /api/shares/{share}/{sessions|mindmaps|notes}/{id}"
    )
    title: str
    parts: int = Field(description="Slides of a deck, chapters of an audio overview; else 0")
    duration_ms: int = Field(description="Narration length; 0 before it is voiced, or for a map")
    created_at: datetime

    @computed_field(description="Its name as it is shown: the title, or `Untitled mind map`…")
    @property
    def display_title(self) -> str:
        return shown(self.title, self.kind)


class SharedItem(BaseModel):
    """One output a share includes, as Discover lists it on its own: to play
    or read without opening its collection first."""

    key: str = Field(description="The output key, as in the share's outputs")
    kind: Literal["slides", "audio", "mindmap", "notes"]
    id: uuid.UUID = Field(
        description="Read it at /api/shares/{share_id}/{sessions|mindmaps|notes}/{id}; "
        "a deck or audio overview plays at /ui/play/{id}?share={share_id}"
    )
    title: str
    parts: int = Field(description="Slides of a deck, chapters of an audio overview; else 0")
    duration_ms: int = Field(description="Narration length; 0 before it is voiced, or for a map")
    created_at: datetime = Field(description="When it was made: the newest order")
    share_id: uuid.UUID
    collection_id: uuid.UUID
    collection_title: str = Field(description="Its collection's current title")
    cover_version: str = Field(
        description="Load its collection's cover from /api/shares/{share_id}/cover with it as `v`"
    )
    shared_by: str = Field(description="The sharer's display name; empty when they have none")
    mine: bool = Field(description="The person asking shared it")
    reuses: int = Field(description="How many times its share was reused: the reused order")

    @computed_field(description="Its name as it is shown: the title, or `Untitled mind map`…")
    @property
    def display_title(self) -> str:
        return shown(self.title, self.kind)

    @computed_field(
        description="Its collection's name as it is shown: the title, or `Untitled collection`"
    )
    @property
    def collection_display_title(self) -> str:
        return collections.display_title(self.collection_title)

    @classmethod
    def of(cls, i: shares.Item, me: uuid.UUID) -> SharedItem:
        return cls(
            key=i.out.key,
            kind=i.out.kind,
            id=i.out.id,
            title=i.out.title,
            parts=i.out.parts,
            duration_ms=i.out.duration_ms,
            created_at=i.out.created_at,
            share_id=i.share_id,
            collection_id=i.collection_id,
            collection_title=i.collection_title,
            cover_version=i.cover_version,
            shared_by=i.shared_by,
            mine=i.owner_id == me,
            reuses=i.reuses,
        )


class SharedItems(BaseModel):
    """One page of Discover's items."""

    items: list[SharedItem]
    next_offset: int | None = Field(
        description="Ask again with it as `offset` for the next page; null on the last"
    )


class ShareView(BaseModel):
    """One shared collection as a visitor sees it."""

    card: ShareCard
    sources: list[SourceOut] = Field(description="Empty when the sources are not included")
    outputs: list[SharedOutput] = Field(description="In the order the owner picked them")


class ShareSet(BaseModel):
    include_sources: bool
    outputs: list[str] = Field(
        default_factory=list[str], max_length=500, description=f"Ready outputs: {OUTPUT_KEY}"
    )
    note: str = Field(default="", max_length=2000, description="Trimmed and cut to 280 characters")
    allow_edits: bool = Field(
        default=False,
        description="Let people change their copies and share them again; "
        "otherwise each copy is read-only",
    )


class SharePatch(BaseModel):
    include_sources: bool | None = None
    outputs: list[str] | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=2000)
    allow_edits: bool | None = None


# ── the feed and a share ──────────────────────────────────────────────────────


@router.get("/shares")
async def list_shares(
    s: Db,
    me: Me,
    query: Annotated[
        str,
        Query(
            max_length=200,
            description="Case-insensitive; matched against the title, the note and the "
            "cover's topic and terms",
        ),
    ] = "",
    sort: Annotated[
        shares.Sort, Query(description="`newest` (the default) or `reused`: most reused first")
    ] = "newest",
) -> list[ShareCard]:
    """Discover: what everyone on the studio shared."""
    return [ShareCard.of(c, me.id) for c in shares.feed(await shares.entries(s), query, sort)]


@router.get("/shares/items")
async def list_shared_items(
    s: Db,
    me: Me,
    kind: Annotated[
        shares.OutputKind | None,
        Query(description="`slides`, `audio`, `mindmap` or `notes`; every kind when absent"),
    ] = None,
    query: Annotated[
        str,
        Query(
            max_length=200,
            description="Case-insensitive; matched against the item's title, and its share "
            "as the feed matches it: the collection's title, the note and the cover's topic "
            "and terms",
        ),
    ] = "",
    sort: Annotated[
        shares.Sort,
        Query(description="`newest` made first (the default) or `reused`: most reused share first"),
    ] = "newest",
    limit: Annotated[int, Query(ge=1, le=shares.ITEMS_MAX)] = 24,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> SharedItems:
    """Discover's items: each ready output a share includes, on its own, from
    the shares of everyone on the studio. Only what a share includes is
    listed, and only while it is there and ready."""
    page = await shares.items(s, kind, query, sort, limit, offset)
    return SharedItems(
        items=[SharedItem.of(i, me.id) for i in page.items], next_offset=page.next_offset
    )


@router.get("/shares/{share_id}")
async def get_share(share_id: uuid.UUID, s: Db, me: Me) -> ShareView:
    """One shared collection as a visitor sees it: what it is and what it
    includes."""
    v = await shares.view(s, share_id)
    return ShareView(
        card=ShareCard.of(v.card, me.id),
        sources=[SourceOut.of(x) for x in v.sources],
        outputs=[
            SharedOutput(
                key=o.key,
                kind=o.kind,
                id=o.id,
                title=o.title,
                parts=o.parts,
                duration_ms=o.duration_ms,
                created_at=o.created_at,
            )
            for o in v.outputs
        ],
    )


@router.get("/shares/{share_id}/cover", response_class=HTMLResponse)
async def read_share_cover(
    share_id: uuid.UUID,
    s: Db,
    me: Me,
    v: Annotated[str, Query(description="The `cover_version` the card was listed with")] = "",
    theme: Annotated[str, Query(description="`light`, else the dark default")] = "dark",
) -> HTMLResponse:
    """A shared collection's cover, as its owner sees it; see the collection's
    cover route."""
    share = await shares.readable(s, share_id)
    html, version = await covers.page(s, share.owner_id, share.collection_id, Theme.parse(theme))
    cache = "private, max-age=31536000, immutable" if v == version else "no-cache"
    return HTMLResponse(html, headers={"Cache-Control": cache, **SANDBOXED})


@router.get("/shares/{share_id}/sources/{name}")
async def read_shared_source(share_id: uuid.UUID, name: str, s: Db, me: Me) -> SourceText:
    """One source of a share that includes its sources, with its text."""
    return SourceText.model_validate(
        await shares.source_of(s, share_id, name), from_attributes=True
    )


@router.get("/shares/{share_id}/sessions/{sid}")
async def read_shared_session(share_id: uuid.UUID, sid: uuid.UUID, s: Db, me: Me) -> SessionDetail:
    """A deck or audio overview a share includes, in full, to play."""
    return SessionDetail.full(await shares.output_of(s, share_id, Session, sid))


@router.get("/shares/{share_id}/mindmaps/{mid}")
async def read_shared_mindmap(share_id: uuid.UUID, mid: uuid.UUID, s: Db, me: Me) -> MindMapOut:
    """A mind map a share includes, to read only."""
    return MindMapOut.model_validate(
        await shares.output_of(s, share_id, MindMap, mid), from_attributes=True
    )


@router.get("/shares/{share_id}/notes/{nid}")
async def read_shared_notes(share_id: uuid.UUID, nid: uuid.UUID, s: Db, me: Me) -> NotesOut:
    """Study notes a share includes, to read only."""
    return NotesOut.full(await shares.output_of(s, share_id, StudyNotes, nid))


@router.post("/shares/{share_id}/reuse", status_code=201)
async def reuse_share(share_id: uuid.UUID, s: Db, me: Me) -> CollectionSummary:
    """Copy what a share includes into a new collection of your own, wholly
    independent of the original: it stays when the original or the share
    goes. When the share does not allow edits the copy is read-only; when it
    does, it is yours to change and share again, and what you change never
    touches the original."""
    copy = await shares.reuse(s, me.id, share_id)
    # Named and its cover designed, if what it holds calls for it.
    await refresh.schedule(s, me.id, copy.id)
    return CollectionSummary.of(await collections.summary(s, me.id, copy.id))


# ── the owner's side ──────────────────────────────────────────────────────────


# What an output without a name is called in the share dialog, by its kind.
KIND_LABELS = {
    "slides": "Narrated slides",
    "audio": "Audio overview",
    "mindmap": "Mind map",
    "notes": "Study notes",
}


class Shareable(BaseModel):
    """One output a share can include: ready, by the key a share names it by."""

    key: str = Field(description=f"What a share names it by: {OUTPUT_KEY}")
    kind: shares.OutputKind
    title: str = Field(description="Its name, or its kind while it has none")
    created_at: datetime


class ShareState(BaseModel):
    """Everything the share dialog shows for one of your collections, and
    what it opens on: the share's own choices, or, for a collection not
    shared yet, everything there is."""

    share: ShareOut | None = Field(description="Its share; null when it is not shared")
    sources: int = Field(description="How many sources it has to include")
    items: list[Shareable] = Field(
        description="Its outputs that can be shared, newest first. Only ready ones: one "
        "still being made has nothing to show yet, and a failed one never will"
    )
    include_sources: bool = Field(description="Whether the dialog opens with the sources on")
    picked: list[str] = Field(description="The keys of the items the dialog opens ticked")
    note: str
    allow_edits: bool = Field(description="Off unless the share says so: a copy is read-only")
    note_max: int = Field(description="The longest note a share keeps, in characters")


@router.get("/collections/{cid}/share")
async def get_collection_share(cid: uuid.UUID, s: Db, me: Me) -> ShareState:
    """One of your collections as the share dialog shows it: its share, if it
    has one, what it holds that a share can include, and what the dialog
    opens on."""
    summary = await collections.summary(s, me.id, cid)
    share = await shares.of_collection(s, me.id, cid)
    items: list[Shareable] = []
    for o in await s.scalars(
        select(Session).where(
            Session.collection_id == cid, Session.owner_id == me.id, Session.state == "ready"
        )
    ):
        kind: shares.OutputKind = "audio" if o.kind == "audio" else "slides"
        items.append(
            Shareable(key=f"session:{o.id}", kind=kind, title=o.title, created_at=o.created_at)
        )
    for m in await s.scalars(
        select(MindMap).where(MindMap.collection_id == cid, MindMap.owner_id == me.id)
    ):
        items.append(
            Shareable(key=f"mindmap:{m.id}", kind="mindmap", title=m.title, created_at=m.created_at)
        )
    for n in await s.scalars(
        select(StudyNotes).where(StudyNotes.collection_id == cid, StudyNotes.owner_id == me.id)
    ):
        items.append(
            Shareable(key=f"notes:{n.id}", kind="notes", title=n.title, created_at=n.created_at)
        )
    for i in items:
        i.title = " ".join(i.title.split()) or KIND_LABELS[i.kind]
    items.sort(key=lambda i: i.created_at, reverse=True)
    keys = [i.key for i in items]
    if share is None:
        return ShareState(
            share=None,
            sources=summary.sources,
            items=items,
            include_sources=summary.sources > 0,
            picked=keys,
            note="",
            allow_edits=False,
            note_max=shares.NOTE_MAX,
        )
    return ShareState(
        share=ShareOut.of(share),
        sources=summary.sources,
        items=items,
        include_sources=share.include_sources and summary.sources > 0,
        picked=[k for k in share.outputs if k in keys],
        note=share.note,
        allow_edits=share.allow_edits,
        note_max=shares.NOTE_MAX,
    )


@router.post("/collections/{cid}/shares")
async def share_collection(cid: uuid.UUID, body: ShareSet, s: Db, me: Me) -> ShareOut:
    """Share a collection with everyone on the studio, or change what its share
    includes: the sources or not, each ready output, and whether copies may be
    edited. A collection has at most one share, so asking again changes it.
    A read-only copy cannot be shared."""
    return ShareOut.of(
        await shares.put(
            s, me.id, cid, body.include_sources, body.outputs, body.note, body.allow_edits
        )
    )


@router.patch("/shares/{share_id}")
async def update_share(share_id: uuid.UUID, body: SharePatch, s: Db, me: Me) -> ShareOut:
    """Change what your share includes; what is not sent stays as it is."""
    return ShareOut.of(
        await shares.change(
            s, me.id, share_id, body.include_sources, body.outputs, body.note, body.allow_edits
        )
    )


@router.delete("/shares/{share_id}", status_code=204)
async def delete_share(share_id: uuid.UUID, s: Db, me: Me) -> None:
    """Stop sharing a collection. Copies people already made stay theirs."""
    await shares.remove(s, me.id, share_id)
