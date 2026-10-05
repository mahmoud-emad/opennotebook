"""DEPRECATED: the 47 methods of the old JSON-RPC API, each forwarded to the
REST handler that does the same work, with the old shapes translated both
ways. Kept for one release after cutover so existing agents keep working
while they move to REST (docs/stack-migration-plan.md §4), then deleted with
`rpc.py` and `rpc_openrpc/`.

The translation, once for all methods:

- ids: an old `sid`/`cid` string is the new UUID. One that is not a UUID was
  never made here, so it is treated as not there.
- absence: where the old method answered `found: false` or `false`, a 404
  from the handler becomes that answer; elsewhere it is an error.
- times: `created_at`/`updated_at` become `created_ms`/`updated_ms`.
- outputs: `kind` "slides" is the old "session", `parts` is `slide_count`,
  `collection_id` is `collection` (or `sid` on a map or notes).

Every method is registered with `@method(domain, name, Params)`. `Params` is
the method's parameters as the old wire named them: `{"req": {...}}` or
`{"sid": "..."}`.
"""

import asyncio
import base64
import binascii
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.ai import client, ledger
from opennotebook.ai.errors import AiError
from opennotebook.api import ask as ask_api
from opennotebook.api import collections as collections_api
from opennotebook.api import mindmaps as mindmaps_api
from opennotebook.api import notes as notes_api
from opennotebook.api import sessions as sessions_api
from opennotebook.api import settings as settings_api
from opennotebook.api import sources as sources_api
from opennotebook.db.models import Job, MindMap, Session, Source, StudyNotes, User
from opennotebook.db.session import release
from opennotebook.domain import settings as config
from opennotebook.domain import sources
from opennotebook.domain.sessions import Line, Part
from opennotebook.errors import Problem, not_found
from opennotebook.script import retrieval
from opennotebook.script.errors import Empty, problem

# ── the registry ─────────────────────────────────────────────────────────────


@dataclass
class Ctx:
    """What a method runs with: its own database session (committed by the
    caller when the method returns), the person asking, and the background
    work to run once the response is sent."""

    s: AsyncSession
    me: User
    tasks: BackgroundTasks


Run = Callable[[Ctx, Any], Awaitable[Any]]


@dataclass(frozen=True)
class Method:
    params: type[BaseModel]
    run: Run


# Domain → method name → method, in the order the old schema listed them.
METHODS: dict[str, dict[str, Method]] = {
    d: {} for d in ("session", "sources", "mindmap", "notes", "settings")
}


def method(domain: str, name: str, params: type[BaseModel]) -> Callable[[Run], Run]:
    def register(run: Run) -> Run:
        METHODS[domain][name] = Method(params, run)
        return run

    return register


class Wire(BaseModel):
    # serde ignored fields it did not know; so does this.
    model_config = ConfigDict(extra="ignore")


class NoParams(Wire):
    pass


class SidParam(Wire):
    sid: str


class CidParam(Wire):
    cid: str


class QueryParam(Wire):
    query: str


# ── translation helpers ──────────────────────────────────────────────────────


def _id(v: str, what: str) -> uuid.UUID:
    """An old id as the new UUID. One that is not a UUID was never made here."""
    try:
        return uuid.UUID(v.strip())
    except ValueError:
        raise not_found(what) from None


def _gone(e: Problem) -> bool:
    return e.status == 404


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _title(raw: str) -> str:
    """A rename's title, refused when empty: nothing would name it."""
    title = " ".join(raw.split())
    if not title:
        raise Problem(
            422, "A title cannot be empty: nothing would name it. Write one, then try again."
        )
    return title


# ── session: collections ─────────────────────────────────────────────────────


class CollectionCreateReq(Wire):
    title: str | None = None


class CollectionCreateIn(Wire):
    req: CollectionCreateReq = CollectionCreateReq()


class CollectionRetitleReq(Wire):
    cid: str
    title: str


class CollectionRetitleIn(Wire):
    req: CollectionRetitleReq


class CollectionPinReq(Wire):
    cid: str
    pinned: bool


class CollectionPinIn(Wire):
    req: CollectionPinReq


def collection_of(c: collections_api.CollectionSummary) -> dict[str, Any]:
    return {
        "cid": str(c.id),
        "title": c.title,
        "title_auto": c.title_auto,
        "created_ms": _ms(c.created_at),
        "updated_ms": _ms(c.updated_at),
        "pinned": c.pinned,
    }


def collection_summary_of(c: collections_api.CollectionSummary) -> dict[str, Any]:
    return {
        **collection_of(c),
        "sources": c.sources,
        "decks": c.decks,
        "audios": c.audios,
        "maps": c.maps,
        "notes": c.notes,
        "preparing": c.preparing,
        "failed": c.failed,
        "cover_version": c.cover_version,
    }


def session_summary_of(o: sessions_api.SessionSummary) -> dict[str, Any]:
    return {
        "sid": str(o.id),
        "title": o.title,
        "state": o.state,
        "slide_count": o.parts,
        "speakers": o.speakers,
        "kind": "audio" if o.kind == "audio" else "session",
        "audio_format": o.audio_format,
        "duration_ms": o.duration_ms,
        "description": o.description,
        "created_ms": _ms(o.created_at),
        "pinned": o.pinned,
        "collection": str(o.collection_id),
        "spent_usd": float(o.spent_usd or 0),
        "spent_known": o.spent_known and o.spent_usd is not None,
    }


@method("session", "collection_create", CollectionCreateIn)
async def collection_create(c: Ctx, p: CollectionCreateIn) -> dict[str, Any]:
    body = collections_api.CollectionCreate(title=p.req.title or "")
    return collection_of(await collections_api.create_collection(body, c.s, c.me))


@method("session", "collection_list", NoParams)
async def collection_list(c: Ctx, _: NoParams) -> dict[str, Any]:
    listed = await collections_api.list_collections(c.s, c.me)
    return {"collections": [collection_summary_of(x) for x in listed]}


@method("session", "collection_get", CidParam)
async def collection_get(c: Ctx, p: CidParam) -> dict[str, Any]:
    try:
        got = await collections_api.get_collection(_id(p.cid, "That collection"), c.s, c.me)
    except Problem as e:
        if _gone(e):
            return {"found": False, "outputs": []}
        raise
    return {
        "found": True,
        "collection": collection_summary_of(got.collection),
        "outputs": [session_summary_of(o) for o in got.outputs],
    }


async def _patch_collection(c: Ctx, cid: str, patch: collections_api.CollectionPatch) -> bool:
    try:
        await collections_api.update_collection(_id(cid, "That collection"), patch, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


@method("session", "collection_retitle", CollectionRetitleIn)
async def collection_retitle(c: Ctx, p: CollectionRetitleIn) -> bool:
    patch = collections_api.CollectionPatch(title=p.req.title)
    return await _patch_collection(c, p.req.cid, patch)


@method("session", "collection_pin", CollectionPinIn)
async def collection_pin(c: Ctx, p: CollectionPinIn) -> bool:
    patch = collections_api.CollectionPatch(pinned=p.req.pinned)
    return await _patch_collection(c, p.req.cid, patch)


@method("session", "collection_cover_refresh", CidParam)
async def collection_cover_refresh(c: Ctx, p: CidParam) -> bool:
    try:
        await collections_api.refresh_cover(_id(p.cid, "That collection"), c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


@method("session", "collection_delete", CidParam)
async def collection_delete(c: Ctx, p: CidParam) -> bool:
    try:
        await collections_api.delete_collection(_id(p.cid, "That collection"), c.s, c.me, c.tasks)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


# ── session: building ────────────────────────────────────────────────────────


class SessionBuildReq(Wire):
    sid: str = ""
    collection: str | None = None
    title: str | None = None
    speakers: int | None = None
    slide_count: int | None = None
    style: str | None = None
    audio_format: str | None = None
    audio_length: str | None = None
    focus: str | None = None


class SessionBuildIn(Wire):
    req: SessionBuildReq


class SpeakerIn(Wire):
    speaker_id: str = ""
    voice_id: str = ""
    display_name: str = ""
    role: str = ""


class SessionPrepareReq(Wire):
    sid: str = ""
    title: str = ""
    resource_dir: str = ""
    speakers: list[SpeakerIn] = Field(default_factory=list[SpeakerIn])
    slide_count: int | None = None
    style: str | None = None
    research_topic: str | None = None
    audio_format: str | None = None
    audio_length: str | None = None
    focus: str | None = None
    collection: str | None = None


class SessionPrepareIn(Wire):
    req: SessionPrepareReq


NO_COLLECTION = (
    "Say which collection to build from: pass its cid as collection, from collection_create."
)


def _build_target(sid: str, collection: str | None) -> uuid.UUID:
    """The collection a build reads. With `collection` given the output gets
    a fresh id of its own, whatever `sid` says; without it, `sid` is the
    collection, as before collections."""
    cid = (collection or "").strip() or sid.strip()
    if not cid:
        raise Problem(422, NO_COLLECTION)
    return _id(cid, "That collection")


def _build_req(
    *,
    title: str | None,
    speakers: int | None,
    slide_count: int | None,
    style: str | None,
    audio_format: str | None,
    audio_length: str | None,
    focus: str | None,
    research: str | None = None,
) -> sessions_api.BuildReq:
    fmt = (audio_format or "").strip() or None
    return sessions_api.BuildReq.model_validate(
        {
            "kind": "audio" if fmt else "slides",
            "title": title or "",
            "speakers": speakers,
            "slide_count": slide_count,
            "style": (style or "").strip() or None,
            "audio_format": fmt,
            "audio_length": (audio_length or "").strip() or None,
            "focus": focus or "",
            "research": research or "",
        }
    )


async def _prepared(c: Ctx, out: sessions_api.SessionSummary) -> dict[str, Any]:
    job = await c.s.scalar(select(Job.id).where(Job.session_id == out.id, Job.kind == "prep"))
    return {"accepted": True, "sid": str(out.id), "prep_job_sid": str(job) if job else ""}


def _build_of(r: SessionBuildReq) -> sessions_api.BuildReq:
    return _build_req(
        title=r.title,
        speakers=r.speakers,
        slide_count=r.slide_count,
        style=r.style,
        audio_format=r.audio_format,
        audio_length=r.audio_length,
        focus=r.focus,
    )


@method("session", "session_build", SessionBuildIn)
async def session_build(c: Ctx, p: SessionBuildIn) -> dict[str, Any]:
    cid = _build_target(p.req.sid, p.req.collection)
    out = await sessions_api.build(cid, _build_of(p.req), c.s, c.me)
    return await _prepared(c, out)


@method("session", "session_estimate", SessionBuildIn)
async def session_estimate(c: Ctx, p: SessionBuildIn) -> dict[str, Any]:
    cid = _build_target(p.req.sid, p.req.collection)
    est = await sessions_api.estimate(cid, _build_of(p.req), c.s, c.me)
    # The old shape: the model and the facts came later, with the REST API.
    return est.model_dump(mode="json", exclude={"model", "facts"})


@method("session", "session_prepare", SessionPrepareIn)
async def session_prepare(c: Ctx, p: SessionPrepareIn) -> dict[str, Any]:
    r = p.req
    if r.resource_dir.strip():
        raise Problem(
            422,
            "A directory on the server is no longer read as sources. Add the sources to a "
            "collection, then call session_build with its cid as collection.",
        )
    cid = _build_target(r.sid, r.collection)
    body = _build_req(
        title=r.title,
        # The voices come from the person's settings; only how many is kept.
        speakers=min(len(r.speakers), 2) or None,
        slide_count=r.slide_count,
        style=r.style,
        audio_format=r.audio_format,
        audio_length=r.audio_length,
        focus=r.focus,
        research=r.research_topic,
    )
    return await _prepared(c, await sessions_api.build(cid, body, c.s, c.me))


# ── session: reading and editing ─────────────────────────────────────────────


class SessionAskReq(Wire):
    sid: str
    question: str
    slide_ordinal: int | None = None


class SessionAskIn(Wire):
    req: SessionAskReq


class SessionRetitleReq(Wire):
    sid: str
    title: str


class SessionRetitleIn(Wire):
    req: SessionRetitleReq


class SessionPinReq(Wire):
    sid: str
    pinned: bool


class SessionPinIn(Wire):
    req: SessionPinReq


def _line_of(sid: str, raw: dict[str, Any]) -> dict[str, Any]:
    ln = Line.of_json(raw)
    out: dict[str, Any] = {
        "line_id": ln.line_id,
        "speaker_id": ln.speaker_id,
        "ordinal": ln.ordinal,
        "text": ln.text,
        "cues": ln.cues,
    }
    if ln.audio_path:
        out["audio_path"] = ln.audio_path
        out["audio_url"] = f"/api/sessions/{sid}/audio/{ln.line_id}"
    if ln.duration_ms is not None:
        out["duration_ms"] = ln.duration_ms
    return out


def _slide_of(sid: str, raw: dict[str, Any]) -> dict[str, Any]:
    part = Part.of_json(raw)
    return {
        "slide_ref": {
            "collection": part.collection,
            "presentation": part.presentation,
            "slide": part.slide,
        },
        "ordinal": part.ordinal,
        "aspect": {"width": part.width, "height": part.height},
        "slide_url": f"/api/sessions/{sid}/slides/{part.ordinal}",
        "title": part.title,
        "lines": [_line_of(sid, ln) for ln in raw.get("lines") or []],
    }


def session_of(d: sessions_api.SessionDetail, prep_job: uuid.UUID | None) -> dict[str, Any]:
    sid = str(d.id)
    out: dict[str, Any] = {
        "sid": sid,
        "title": d.title,
        # The old memory collection was named after the session.
        "collection_name": sid,
        "speakers": [SpeakerIn.model_validate(sp).model_dump() for sp in d.speaker_list],
        "slides": [_slide_of(sid, p) for p in d.slides],
        "state": d.state,
        "collection": str(d.collection_id),
    }
    if prep_job is not None:
        out["prep_job_sid"] = str(prep_job)
    if d.state == "failed" and d.failure:
        out["failure"] = d.failure
    if d.audio:
        out["audio"] = d.audio
    if d.spent_usd is not None:
        out["spent_usd"] = float(d.spent_usd)
    if d.style:
        out["style"] = d.style
    return out


@method("session", "session_get", SidParam)
async def session_get(c: Ctx, p: SidParam) -> dict[str, Any]:
    try:
        sid = _id(p.sid, "That output")
        got = await sessions_api.get_session(sid, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return {"found": False}
        raise
    job = await c.s.scalar(select(Job.id).where(Job.session_id == sid, Job.kind == "prep"))
    return {"found": True, "session": session_of(got, job)}


@method("session", "session_list", NoParams)
async def session_list(c: Ctx, _: NoParams) -> dict[str, Any]:
    return {
        "sessions": [session_summary_of(o) for o in await sessions_api.list_sessions(c.s, c.me)]
    }


# How many passages and answers an answer about an output reads, as before.
ASK_TOP_K = 4


@method("session", "session_ask", SessionAskIn)
async def session_ask(c: Ctx, p: SessionAskIn) -> dict[str, Any]:
    """A short spoken-style answer about a built output, from the passages of
    its collection's sources most about the question. There is no REST route
    for it: the web app asks a collection, or asks aloud while listening."""
    question = p.req.question.strip()
    if not question:
        raise Problem(422, "The question is empty. Write a question, then ask again.")
    sid = _id(p.req.sid, "That output")
    o = await c.s.scalar(select(Session).where(Session.id == sid, Session.owner_id == c.me.id))
    if o is None:
        raise not_found("That output")
    if o.state != "ready":
        raise Problem(
            409, "That output is not ready yet. Ask once session_get says its state is ready."
        )
    grounding = await retrieval.retrieve(
        retrieval.Scope(c.me.id, o.collection_id), question, ASK_TOP_K
    )
    on_slide = ""
    i = p.req.slide_ordinal
    if i is not None and 0 <= i < len(o.slides):
        part = Part.of_json(o.slides[i])
        said = " ".join(ln.text for ln in part.lines_in_order())
        on_slide = (
            f'The listener is on the slide "{part.title}", where the narration said: {said}\n\n'
        )
    model = await config.value(c.s, c.me.id, config.CHAT_MODEL_KEY)
    rule = config.language_rule(await config.value(c.s, c.me.id, config.LANGUAGE_KEY))
    # Nothing is written: the transaction goes before the model is asked.
    await release(c.s)
    system = (
        f'You answer a listener\'s question about a narrated learning session titled "{o.title}". '
        "Answer in two to four plain sentences, as you would say them aloud. Use only the "
        f"material given; when it does not cover the question, say so in one sentence. {rule}"
    ).strip()
    user = (
        f"{on_slide}Material from the session's sources:\n\n{grounding.as_context()}\n"
        f"Question: {question}"
    )
    try:
        async with ledger.spending(c.me.id, "ask", collection_id=o.collection_id, session_id=o.id):
            done = await client.ai().complete(
                model, [{"role": "system", "content": system}, {"role": "user", "content": user}]
            )
    except AiError as e:
        raise problem(e) from e
    answer = done.text.strip()
    if not answer:
        raise problem(Empty("answer"))
    return {"answer": answer}


@method("session", "session_delete", SidParam)
async def session_delete(c: Ctx, p: SidParam) -> bool:
    try:
        await sessions_api.delete_session(_id(p.sid, "That output"), c.s, c.me, c.tasks)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


async def _patch_session(c: Ctx, sid: str, patch: sessions_api.SessionPatch) -> bool:
    try:
        await sessions_api.update_session(_id(sid, "That output"), patch, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


@method("session", "session_retitle", SessionRetitleIn)
async def session_retitle(c: Ctx, p: SessionRetitleIn) -> bool:
    patch = sessions_api.SessionPatch(title=_title(p.req.title))
    return await _patch_session(c, p.req.sid, patch)


@method("session", "session_pin", SessionPinIn)
async def session_pin(c: Ctx, p: SessionPinIn) -> bool:
    return await _patch_session(c, p.req.sid, sessions_api.SessionPatch(pinned=p.req.pinned))


# ── session: playback ────────────────────────────────────────────────────────


class PlaybackAtReq(Wire):
    sid: str
    slide_ordinal: int
    line_id: str
    offset_ms: int


class PlaybackAtIn(Wire):
    req: PlaybackAtReq


@method("session", "playback_get", SidParam)
async def playback_get(c: Ctx, p: SidParam) -> dict[str, Any]:
    head = await sessions_api.get_playback(_id(p.sid, "That output"), c.s, c.me)
    return head.model_dump()


async def _play(c: Ctx, r: PlaybackAtReq, state: str) -> dict[str, Any]:
    head = sessions_api.Playhead.model_validate(
        {
            "slide_ordinal": r.slide_ordinal,
            "line_id": r.line_id,
            "offset_ms": r.offset_ms,
            "state": state,
        }
    )
    out = await sessions_api.set_playback(_id(r.sid, "That output"), head, c.s, c.me)
    return out.model_dump()


@method("session", "playback_play", PlaybackAtIn)
async def playback_play(c: Ctx, p: PlaybackAtIn) -> dict[str, Any]:
    return await _play(c, p.req, "playing")


@method("session", "playback_pause", PlaybackAtIn)
async def playback_pause(c: Ctx, p: PlaybackAtIn) -> dict[str, Any]:
    return await _play(c, p.req, "paused")


@method("session", "playback_progress", PlaybackAtIn)
async def playback_progress(c: Ctx, p: PlaybackAtIn) -> dict[str, Any]:
    return await _play(c, p.req, "playing")


@method("session", "playback_finish", SidParam)
async def playback_finish(c: Ctx, p: SidParam) -> dict[str, Any]:
    sid = _id(p.sid, "That output")
    head = await sessions_api.get_playback(sid, c.s, c.me)
    head.state = "finished"
    return (await sessions_api.set_playback(sid, head, c.s, c.me)).model_dump()


# ── sources ──────────────────────────────────────────────────────────────────


class SourceAddUrlsReq(Wire):
    sid: str
    urls: list[str]


class SourceAddUrlsIn(Wire):
    req: SourceAddUrlsReq


class SourceAddTextReq(Wire):
    sid: str
    text: str
    title: str | None = None


class SourceAddTextIn(Wire):
    req: SourceAddTextReq


class SourceAddFileReq(Wire):
    sid: str
    name: str
    data_base64: str


class SourceAddFileIn(Wire):
    req: SourceAddFileReq


class SourceRemoveReq(Wire):
    sid: str
    name: str


class SourceRemoveIn(Wire):
    req: SourceRemoveReq


class DeepResearchReq(Wire):
    sid: str
    topic: str


class DeepResearchIn(Wire):
    req: DeepResearchReq


class SourceAskReq(Wire):
    sid: str
    question: str
    sources: list[str] | None = None


class SourceAskIn(Wire):
    req: SourceAskReq


def add_result_of(r: sources_api.AddResult) -> dict[str, Any]:
    src = r.source
    return {
        "url": r.url,
        "ok": r.ok,
        "title": src.title if src else "",
        "chars": src.chars if src else 0,
        "error": r.error,
    }


@method("sources", "draft_create", NoParams)
async def draft_create(c: Ctx, _: NoParams) -> str:
    made = await collections_api.create_collection(
        collections_api.CollectionCreate(title=""), c.s, c.me
    )
    return str(made.id)


@method("sources", "source_add_urls", SourceAddUrlsIn)
async def source_add_urls(c: Ctx, p: SourceAddUrlsIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    body = sources_api.AddUrls.model_validate({"kind": "urls", "urls": p.req.urls})
    out = await sources_api.add_sources(cid, body, c.s, c.me)
    return {"added": sum(r.ok for r in out), "results": [add_result_of(r) for r in out]}


@method("sources", "source_add_text", SourceAddTextIn)
async def source_add_text(c: Ctx, p: SourceAddTextIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    body = sources_api.AddNote(kind="text", text=p.req.text, title=p.req.title or "")
    try:
        (r,) = await sources_api.add_sources(cid, body, c.s, c.me)
    except Problem as e:
        # A note that is not kept is a result that says why, as a page is.
        if e.status != 422:
            raise
        return {"url": "", "ok": False, "title": "", "chars": 0, "error": e.detail}
    return add_result_of(r)


def base64_chars(n: int) -> int:
    """How long `n` bytes are as padded base64."""
    return -(-n // 3) * 4


@method("sources", "source_add_file", SourceAddFileIn)
async def source_add_file(c: Ctx, p: SourceAddFileIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    name = p.req.name.strip()
    if not name:
        raise Problem(422, "Give the file its name, extension included, so it can be read.")
    # Measured before decoding, so an oversized file is never decoded.
    if len(p.req.data_base64) > base64_chars(sources.MAX_UPLOAD_BYTES):
        raise Problem(413, f"{name} is larger than 25 MB. Split it, or upload the part you need.")
    try:
        data = base64.b64decode(p.req.data_base64, validate=True)
    except binascii.Error, ValueError:
        raise Problem(
            422,
            "The file's data_base64 is not valid base64. Encode the file's bytes and try again.",
        ) from None
    upload = UploadFile(io.BytesIO(data), filename=name, size=len(data))
    (r,) = await sources_api.add_files(cid, c.s, c.me, [upload])
    return add_result_of(r)


@method("sources", "source_list", SidParam)
async def source_list(c: Ctx, p: SidParam) -> dict[str, Any]:
    listed = await sources_api.list_sources(_id(p.sid, "That collection"), c.s, c.me)
    return {
        "sources": [
            {"name": x.name, "title": x.title, "url": x.url, "chars": x.chars} for x in listed
        ]
    }


@method("sources", "source_remove", SourceRemoveIn)
async def source_remove(c: Ctx, p: SourceRemoveIn) -> bool:
    try:
        cid = _id(p.req.sid, "That collection")
        await sources_api.remove_source(cid, p.req.name, c.s, c.me, c.tasks)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


@method("sources", "web_search", QueryParam)
async def web_search(c: Ctx, p: QueryParam) -> dict[str, Any]:
    query = p.query.strip()
    if not query:
        raise Problem(422, "The search is empty. Write what to look for, then search again.")
    body = sources_api.SearchReq(query=query)
    hits = await sources_api.web_search(body, c.s, c.me)
    return {"hits": [h.model_dump() for h in hits]}


# How long deep_research waits for its job, and how often it looks. The old
# method held the request until the report was written; research is a job
# now, and this waits for it the same way.
RESEARCH_WAIT_S = 15 * 60
RESEARCH_POLL_S = 1.0


@method("sources", "deep_research", DeepResearchIn)
async def deep_research(c: Ctx, p: DeepResearchIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    topic = p.req.topic.strip()
    if not topic:
        raise Problem(422, "The topic is empty. Write what to research, then try again.")
    body = sources_api.ResearchReq(topic=topic)
    job = await sources_api.deep_research(cid, body, c.s, c.me)
    # The job is queued once this commits; nothing is held open while it runs.
    await c.s.commit()
    status, error = job.status, job.error
    try:
        async with asyncio.timeout(RESEARCH_WAIT_S):
            while status not in ("done", "failed", "cancelled"):
                await asyncio.sleep(RESEARCH_POLL_S)
                row = (
                    await c.s.execute(select(Job.status, Job.error).where(Job.id == job.id))
                ).first()
                await c.s.rollback()
                if row is None:
                    status, error = "cancelled", None
                else:
                    status, error = row.status, row.error
    except TimeoutError:
        return {
            "url": "",
            "ok": False,
            "title": "",
            "chars": 0,
            "error": f"The research is still running after {RESEARCH_WAIT_S // 60} minutes. "
            "Its report is added to the collection when it is done: check source_list later.",
        }
    if status != "done":
        said = error or "The research stopped before it was done. Try again."
        return {"url": "", "ok": False, "title": "", "chars": 0, "error": said}
    report = await c.s.scalar(
        select(Source)
        .where(
            Source.collection_id == cid,
            Source.owner_id == c.me.id,
            Source.kind == "research",
            Source.created_at >= job.created_at,
        )
        .order_by(Source.created_at.desc())
        .limit(1)
    )
    if report is None:
        return {
            "url": "",
            "ok": False,
            "title": "",
            "chars": 0,
            "error": "The research finished without a report. Try again.",
        }
    return {"url": "", "ok": True, "title": report.title, "chars": report.chars, "error": ""}


@method("sources", "source_ask", SourceAskIn)
async def source_ask(c: Ctx, p: SourceAskIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    question = p.req.question.strip()
    if not question:
        raise Problem(422, "The question is empty. Write a question, then ask again.")
    body = ask_api.AskReq(question=question, sources=p.req.sources)
    return (await ask_api.ask_sources(cid, body, c.s, c.me)).model_dump()


# ── mind maps ────────────────────────────────────────────────────────────────


class MakeReq(Wire):
    sid: str
    focus: str | None = None
    sources: list[str] | None = None


class MakeIn(Wire):
    req: MakeReq


class ItemRef(Wire):
    sid: str
    id: str


class ItemRefIn(Wire):
    req: ItemRef


class ItemRetitle(Wire):
    sid: str
    id: str
    title: str


class ItemRetitleIn(Wire):
    req: ItemRetitle


def mindmap_summary_of(m: mindmaps_api.MindMapSummary) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "sid": str(m.collection_id),
        "title": m.title,
        "focus": m.focus,
        "node_count": m.node_count,
        "created_ms": _ms(m.created_at),
        "sources": m.sources,
        "shape": m.shape,
    }


def mindmap_of(m: mindmaps_api.MindMapOut) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "sid": str(m.collection_id),
        "title": m.title,
        "focus": m.focus,
        "sources": m.sources,
        "excerpted": m.excerpted,
        "model": m.model,
        "created_ms": _ms(m.created_at),
        "node_count": m.node_count,
        "dropped": m.dropped,
        "unchecked": m.unchecked,
        "root": m.root.model_dump(),
    }


def _ref(r: ItemRef | ItemRetitle, what: str) -> tuple[uuid.UUID, uuid.UUID]:
    return _id(r.sid, "That collection"), _id(r.id, what)


def _make(r: MakeReq) -> mindmaps_api.MakeReq:
    return mindmaps_api.MakeReq(focus=r.focus or "", sources=r.sources)


@method("mindmap", "mindmap_create", MakeIn)
async def mindmap_create(c: Ctx, p: MakeIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    return mindmap_of(await mindmaps_api.make_mindmap(cid, _make(p.req), c.s, c.me))


@method("mindmap", "mindmap_estimate", SidParam)
async def mindmap_estimate(c: Ctx, p: SidParam) -> dict[str, Any]:
    cid = _id(p.sid, "That collection")
    # The old shape: the limit fields came later, with the REST API.
    return (await mindmaps_api.one_call(c.s, c.me.id, cid)).model_dump(
        exclude={"limit_usd", "over_limit"}
    )


@method("mindmap", "mindmap_list", SidParam)
async def mindmap_list(c: Ctx, p: SidParam) -> dict[str, Any]:
    listed = await mindmaps_api.list_mindmaps(_id(p.sid, "That collection"), c.s, c.me)
    return {"maps": [mindmap_summary_of(m) for m in listed]}


@method("mindmap", "mindmap_list_all", NoParams)
async def mindmap_list_all(c: Ctx, _: NoParams) -> dict[str, Any]:
    rows = await c.s.scalars(
        select(MindMap).where(MindMap.owner_id == c.me.id).order_by(MindMap.created_at.desc())
    )
    return {
        "maps": [
            mindmap_summary_of(mindmaps_api.MindMapSummary.model_validate(m, from_attributes=True))
            for m in rows
        ]
    }


@method("mindmap", "mindmap_get", ItemRefIn)
async def mindmap_get(c: Ctx, p: ItemRefIn) -> dict[str, Any]:
    cid, mid = _ref(p.req, "That mind map")
    return mindmap_of(await mindmaps_api.get_mindmap(cid, mid, c.s, c.me))


@method("mindmap", "mindmap_delete", ItemRefIn)
async def mindmap_delete(c: Ctx, p: ItemRefIn) -> bool:
    try:
        cid, mid = _ref(p.req, "That mind map")
        await mindmaps_api.delete_mindmap(cid, mid, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


@method("mindmap", "mindmap_retitle", ItemRetitleIn)
async def mindmap_retitle(c: Ctx, p: ItemRetitleIn) -> bool:
    body = mindmaps_api.Retitle(title=_title(p.req.title))
    try:
        cid, mid = _ref(p.req, "That mind map")
        await mindmaps_api.retitle_mindmap(cid, mid, body, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


# ── notes ────────────────────────────────────────────────────────────────────


def notes_summary_of(n: notes_api.NotesSummary) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "sid": str(n.collection_id),
        "title": n.title,
        "focus": n.focus,
        "created_ms": _ms(n.created_at),
        "sources": n.sources,
        "ideas": n.ideas,
        "questions": n.questions,
        "terms": n.terms,
        "headings": n.headings,
    }


def notes_of(n: notes_api.NotesOut) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "sid": str(n.collection_id),
        "title": n.title,
        "focus": n.focus,
        "sources": n.sources,
        "excerpted": n.excerpted,
        "model": n.model,
        "created_ms": _ms(n.created_at),
        "dropped": n.dropped,
        "unchecked": n.unchecked,
        "overview": n.overview,
        "ideas": [i.model_dump() for i in n.idea_list],
        "quiz": [q.model_dump() for q in n.quiz],
        "essays": n.essays,
        "glossary": [t.model_dump() for t in n.glossary],
        "citations": [x.model_dump() for x in n.citations],
        "markdown": n.markdown,
    }


@method("notes", "notes_create", MakeIn)
async def notes_create(c: Ctx, p: MakeIn) -> dict[str, Any]:
    cid = _id(p.req.sid, "That collection")
    return notes_of(await notes_api.make_notes(cid, _make(p.req), c.s, c.me))


@method("notes", "notes_estimate", SidParam)
async def notes_estimate(c: Ctx, p: SidParam) -> dict[str, Any]:
    cid = _id(p.sid, "That collection")
    # The old shape: the limit fields came later, with the REST API.
    return (await notes_api.one_call(c.s, c.me.id, cid)).model_dump(
        exclude={"limit_usd", "over_limit"}
    )


@method("notes", "notes_list", SidParam)
async def notes_list(c: Ctx, p: SidParam) -> dict[str, Any]:
    listed = await notes_api.list_notes(_id(p.sid, "That collection"), c.s, c.me)
    return {"notes": [notes_summary_of(n) for n in listed]}


@method("notes", "notes_list_all", NoParams)
async def notes_list_all(c: Ctx, _: NoParams) -> dict[str, Any]:
    rows = await c.s.scalars(
        select(StudyNotes)
        .where(StudyNotes.owner_id == c.me.id)
        .order_by(StudyNotes.created_at.desc())
    )
    return {"notes": [notes_summary_of(notes_api.NotesSummary.of(n)) for n in rows]}


@method("notes", "notes_get", ItemRefIn)
async def notes_get(c: Ctx, p: ItemRefIn) -> dict[str, Any]:
    cid, nid = _ref(p.req, "Those notes")
    return notes_of(await notes_api.get_notes(cid, nid, c.s, c.me))


@method("notes", "notes_delete", ItemRefIn)
async def notes_delete(c: Ctx, p: ItemRefIn) -> bool:
    try:
        cid, nid = _ref(p.req, "Those notes")
        await notes_api.delete_notes(cid, nid, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


@method("notes", "notes_retitle", ItemRetitleIn)
async def notes_retitle(c: Ctx, p: ItemRetitleIn) -> bool:
    body = mindmaps_api.Retitle(title=_title(p.req.title))
    try:
        cid, nid = _ref(p.req, "Those notes")
        await notes_api.retitle_notes(cid, nid, body, c.s, c.me)
    except Problem as e:
        if _gone(e):
            return False
        raise
    return True


# ── settings ─────────────────────────────────────────────────────────────────


class SettingSetReq(Wire):
    key: str
    value: str


class SettingSetIn(Wire):
    req: SettingSetReq


def setting_of(x: settings_api.Setting) -> dict[str, Any]:
    out = x.model_dump(exclude={"suggestions", "scope", "min", "max"})
    if x.min is not None:
        out["min"] = x.min
    if x.max is not None:
        out["max"] = x.max
    return out


@method("settings", "settings_get", NoParams)
async def settings_get(c: Ctx, _: NoParams) -> dict[str, Any]:
    got = await settings_api.get_settings(c.s, c.me)
    return {
        "tabs": [t.model_dump() for t in got.tabs],
        "settings": [setting_of(x) for x in got.settings],
    }


@method("settings", "settings_set", SettingSetIn)
async def settings_set(c: Ctx, p: SettingSetIn) -> dict[str, Any]:
    body = settings_api.SetValue(value=p.req.value)
    saved = await settings_api.set_setting(p.req.key, body, c.s, c.me)
    return {"setting": setting_of(saved), "note": ""}


@method("settings", "styles_list", NoParams)
async def styles_list(c: Ctx, _: NoParams) -> dict[str, Any]:
    # The old shape: the pictures came later, with the REST API.
    return {
        "styles": [
            x.model_dump(exclude={"thumbnail"}) for x in await settings_api.list_styles(c.me)
        ]
    }
