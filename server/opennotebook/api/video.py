"""An output's videos: make one, follow it, play it, download it, and read
its captions (`docs/video-overview-spec.md`).

A video is made from a finished output and kept beside it, one per style.
Its state lives on the output's `video` field, apart from the output's own,
so a render that fails leaves a ready deck ready. The bytes come from the
files volume with range requests, so a player can seek before the download
ends.

Each read route comes twice, as the media routes do: under
`/api/sessions/{sid}` for the output's owner, and under
`/api/shares/{share}/sessions/{sid}` for anyone signed in, only for an
output the share includes.
"""

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import storage
from opennotebook.ai import client, ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.api.media import file_name_of
from opennotebook.api.notes import Citation
from opennotebook.build import video
from opennotebook.db.models import Job, Session
from opennotebook.db.session import release
from opennotebook.domain import reading, sessions, shares
from opennotebook.domain import settings as config
from opennotebook.errors import Problem, not_found
from opennotebook.script import explain
from opennotebook.script.errors import ScriptError, problem

router = APIRouter(prefix="/api", tags=["video"])

Style = Literal["slides", "whiteboard"]
StyleQuery = Annotated[Style, Query(description="The video's style")]
# A whiteboard's look (build/whiteboard/theme.py): one per theme the studio
# has, kept in step with it by a test.
ThemeId = Literal["whiteboard", "notebook", "chalkboard", "blueprint"]

NOT_READY = "This output is still being made. Make its video once it is ready."
NO_VIDEO = "This output has no video in that style yet. Make one first."
RENDERING = "The video is still being made. Wait for it to finish, then try again."
NO_KEY = (
    "The studio has no AI key, and a whiteboard video's scenes are written by a model. Add "
    "OPENNOTEBOOK_AI_KEY to the server's environment, then try again."
)
MADE_FROM_FAILED = (
    "The output this video was to be made from could not be made, so neither could the video. "
    "Make it again."
)
LOST = "The video stopped before it was finished. Make it again to start over."
MISSING = "This video is missing from the studio's files. Make it again."

MP4: dict[int | str, dict[str, Any]] = {
    200: {"content": {"video/mp4": {}}, "description": "An MP4 file"},
    206: {"description": "The byte range asked for"},
}
VTT: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/vtt": {}}, "description": "The narration as WebVTT captions"}
}


class VideoState(BaseModel):
    """One style's video of an output, and how far it is."""

    style: Style
    state: Literal["none", "waiting", "rendering", "ready", "failed"] = Field(
        description="`waiting`: asked for with its output, and starts when that is ready"
    )
    job_id: uuid.UUID | None = Field(default=None, description="The render's job, to follow")
    failure: str | None = Field(default=None, description="Why it failed, in a sentence")
    bytes: int = 0
    duration_ms: int = 0
    chapters: int = 0
    scenes: int = Field(default=0, description="A whiteboard's scenes")
    first_try: int = Field(
        default=0, description="A whiteboard's scenes that passed every check as first written"
    )
    plain: int = Field(
        default=0, description="A whiteboard's scenes drawn plain after their repair failed"
    )
    repaired: int = Field(default=0, description="A whiteboard's scenes fixed by a repair round")
    escalated: int = Field(
        default=0, description="A whiteboard's scenes written again by the stronger model"
    )
    texts: int = Field(default=0, description="Pieces of text a whiteboard writes on its board")
    grounded: int = Field(
        default=0, description="Of those, the ones the narration or the sources say"
    )
    claims: int = Field(default=0, description="What a whiteboard's scenes assert")
    supported: int = Field(default=0, description="Of those, the ones the check found supported")
    unchecked: int = Field(
        default=0, description="Scenes the check could not be made for (the checker was down)"
    )
    spent_usd: float | None = Field(default=None, description="What the render's model calls cost")
    measured_words: bool = Field(
        default=False,
        description="Every word's time came from the speech server rather than an estimate",
    )
    rendered_at: datetime | None = None
    theme: str | None = Field(
        default=None, description="A whiteboard's theme; null for slides and older videos"
    )
    playable: bool = Field(
        default=False,
        description="A video can be played now: this one, or while a new one is made or "
        "after it failed, the one made before it",
    )
    waiting: str | None = Field(
        default=None,
        description="Why a render has not started, in a sentence: it is queued and no worker "
        "is running. Null otherwise",
    )

    @classmethod
    def of(cls, style: Style, raw: Any) -> VideoState:
        v: dict[str, Any] = raw if isinstance(raw, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        if not v:
            return cls(style=style, state="none")
        playable = v.get("state") == "ready" or isinstance(v.get("previous"), dict)
        return cls.model_validate({**v, "style": style, "playable": playable})


class VideoReq(BaseModel):
    style: Style = "slides"
    theme: ThemeId = Field(default="whiteboard", description="A whiteboard's look")


async def _owned(s: AsyncSession, owner: uuid.UUID, sid: uuid.UUID, lock: bool = False) -> Session:
    q = select(Session).where(Session.id == sid, Session.owner_id == owner)
    o = await s.scalar(q.with_for_update() if lock else q)
    if o is None:
        raise not_found("That output")
    return o


async def state_of(s: AsyncSession, o: Session, style: Style) -> VideoState:
    """A style's state, with a render whose job ended without writing its
    outcome (the worker was killed, the job was stopped) said as failed
    rather than left rendering forever."""
    st = VideoState.of(style, (o.video or {}).get(style))
    if st.state == "waiting":
        if o.state == "failed":
            st.state, st.failure = "failed", MADE_FROM_FAILED
        elif o.state == "ready" and not await sessions.worker_alive(s):
            st.waiting = sessions.WAITING
        return st
    if st.state != "rendering" or st.job_id is None:
        return st
    alive, queued = await _alive(s, st.job_id)
    if not alive:
        st.state, st.failure = "failed", LOST
    elif queued and not await sessions.worker_alive(s):
        st.waiting = sessions.WAITING
    return st


async def _alive(s: AsyncSession, job_id: uuid.UUID) -> tuple[bool, bool]:
    """Whether a render's job is still going, and whether it is still
    waiting its turn: our row, corrected by the queue's own record, as a
    build's is (`sessions.job_status`). A row the queue no longer has, or one
    it ended without our row saying so (an old worker without the task, a
    write that failed), is not going."""
    job = await s.get(Job, job_id)
    # `done` with the state still rendering: the outcome was never written,
    # and nothing will write it now.
    if job is None or job.status in ("failed", "cancelled", "done"):
        return False, False
    if job.procrastinate_job_id is not None:
        queued = await s.scalar(
            text("SELECT status::text FROM procrastinate_jobs WHERE id = :id"),
            {"id": job.procrastinate_job_id},
        )
        if queued is None or queued in ("failed", "cancelled", "aborted", "succeeded"):
            return False, False
    return True, job.status == "queued"


async def _start(
    s: AsyncSession, o: Session, style: Style, theme: str = "whiteboard"
) -> VideoState:
    if o.state != "ready":
        raise Problem(409, NOT_READY)
    current = await state_of(s, o, style)
    if current.state == "rendering":
        # Already on its way: the same render, not a second one.
        return current
    try:
        video.tool(video.FFMPEG_KEY, "ffmpeg")
        video.tool(video.FFPROBE_KEY, "ffprobe")
    except video.ToolMissing as e:
        # Refused at once rather than queued: a render with no encoder
        # fails at its end, after drawing every slide.
        raise Problem(503, e.sentence) from e
    if style == "whiteboard" and not client.ai().has_key:
        # The scenes are written by a model: without a key the render would
        # fail at its first call, after waiting its turn.
        raise Problem(503, NO_KEY)
    await video.queue(s, o, style, theme)
    return VideoState.of(style, (o.video or {})[style])


@router.post("/sessions/{sid}/video", status_code=202)
async def make_video(sid: uuid.UUID, body: VideoReq, s: Db, me: Me) -> VideoState:
    """Make a video of a finished output: its narration over its slides (an
    audio overview's chapters as title cards), with chapters and a caption
    track. Takes about a minute; follow it on /api/jobs/{job_id}. Asking
    again while one is being made returns that one."""
    return await _start(s, await _owned(s, me.id, sid, lock=True), body.style, body.theme)


@router.get("/sessions/{sid}/videos")
async def list_videos(sid: uuid.UUID, s: Db, me: Me) -> list[VideoState]:
    """Every style's video of an output and how far it is."""
    o = await _owned(s, me.id, sid)
    return [await state_of(s, o, st) for st in video.STYLES]  # pyright: ignore[reportArgumentType]


async def _ready(s: AsyncSession, o: Session, style: Style) -> dict[str, Any]:
    """The video to serve: this style's, or while a new one is made or after
    it failed, the one made before it."""
    st = await state_of(s, o, style)
    raw: dict[str, Any] = (o.video or {}).get(style) or {}
    if st.state == "ready":
        return raw
    previous = raw.get("previous")
    if isinstance(previous, dict):
        return previous  # pyright: ignore[reportUnknownVariableType]
    if st.state == "rendering":
        raise Problem(409, RENDERING)
    raise Problem(404, NO_VIDEO)


def _file(rel: Any) -> str:
    if not isinstance(rel, str) or not rel or not storage.exists(rel):
        raise Problem(404, MISSING)
    return str(storage.local_path(rel))


async def video_response(s: AsyncSession, o: Session, style: Style, download: bool) -> Response:
    v = await _ready(s, o, style)
    name = file_name_of(file_name_of(o.title).encode("ascii", "ignore").decode())
    return FileResponse(
        _file(v.get("path")),
        media_type="video/mp4",
        filename=f"{name}.mp4",
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


async def captions_response(s: AsyncSession, o: Session, style: Style) -> Response:
    v = await _ready(s, o, style)
    return FileResponse(
        _file(v.get("captions")),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "private, max-age=3600"},
    )


Download = Annotated[bool, Query(description="Send it as a download rather than to play")]


@router.get("/sessions/{sid}/video", response_class=Response, responses=MP4)
async def get_video(
    sid: uuid.UUID, s: Db, me: Me, style: StyleQuery = "slides", download: Download = False
) -> Response:
    """The video, to play (with range requests) or to download."""
    return await video_response(s, await _owned(s, me.id, sid), style, download)


@router.get("/sessions/{sid}/video/captions", response_class=Response, responses=VTT)
async def get_captions(sid: uuid.UUID, s: Db, me: Me, style: StyleQuery = "slides") -> Response:
    """The video's narration as WebVTT captions."""
    return await captions_response(s, await _owned(s, me.id, sid), style)


@router.get("/shares/{share_id}/sessions/{sid}/video", response_class=Response, responses=MP4)
async def shared_video(
    share_id: uuid.UUID,
    sid: uuid.UUID,
    s: Db,
    me: Me,
    style: StyleQuery = "slides",
    download: Download = False,
) -> Response:
    """The video of an output a share includes."""
    o = await shares.output_of(s, share_id, Session, sid)
    return await video_response(s, o, style, download)


@router.get(
    "/shares/{share_id}/sessions/{sid}/video/captions", response_class=Response, responses=VTT
)
async def shared_captions(
    share_id: uuid.UUID, sid: uuid.UUID, s: Db, me: Me, style: StyleQuery = "slides"
) -> Response:
    """The captions of a video of an output a share includes."""
    o = await shares.output_of(s, share_id, Session, sid)
    return await captions_response(s, o, style)


# ── the watch page: the video's script, and its explainer ────────────────────


class ScriptChapter(BaseModel):
    title: str
    start_ms: int
    end_ms: int


class ScriptLine(BaseModel):
    start_ms: int
    end_ms: int
    text: str


class ScriptScene(BaseModel):
    title: str = ""
    start_ms: int
    end_ms: int
    labels: list[str] = Field(default_factory=list[str], description="What its board writes")
    claims: list[str] = Field(default_factory=list[str], description="What it asserts")


class VideoScript(BaseModel):
    """What a video says and shows, and when."""

    title: str
    duration_ms: int
    chapters: list[ScriptChapter]
    lines: list[ScriptLine] = Field(description="The narration, line by line")
    scenes: list[ScriptScene] = Field(description="A whiteboard's scenes; empty for slides")


CUE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(\d+):(\d{2}):(\d{2})[.,](\d{3})")


def _cue_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def lines_of_vtt(text: str) -> list[ScriptLine]:
    """A video's captions as its narration: what a video made before scripts
    were kept is followed by."""
    out: list[ScriptLine] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        rows = block.strip().split("\n")
        for i, row in enumerate(rows):
            mt = CUE.search(row)
            if mt:
                said = " ".join(r.strip() for r in rows[i + 1 :] if r.strip())
                if said:
                    g = mt.groups()
                    start, end = _cue_ms(*g[:4]), _cue_ms(*g[4:])
                    out.append(ScriptLine(start_ms=start, end_ms=end, text=said))
                break
    return _sentences(out)


# A caption is a few words; a line of the transcript is a sentence, or this
# many words of a long one.
LINE_WORDS = 40


def _sentences(cues: list[ScriptLine]) -> list[ScriptLine]:
    """Captions joined into whole sentences, as the transcript reads them."""
    out: list[ScriptLine] = []
    cur: ScriptLine | None = None
    for c in cues:
        if cur is None:
            cur = c.model_copy()
        else:
            cur.text = f"{cur.text} {c.text}"
            cur.end_ms = c.end_ms
        if cur.text.rstrip().endswith((".", "!", "?")) or len(cur.text.split()) >= LINE_WORDS:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


async def script_of(s: AsyncSession, o: Session, style: Style) -> VideoScript:
    v = await _ready(s, o, style)
    rel = v.get("script")
    if isinstance(rel, str) and storage.exists(rel):
        raw = await asyncio.to_thread(storage.local_path(rel).read_text, encoding="utf-8")
        return VideoScript.model_validate(json.loads(raw))
    caps = await asyncio.to_thread(Path(_file(v.get("captions"))).read_text, encoding="utf-8")
    lines = lines_of_vtt(caps)
    total = int(v.get("duration_ms") or (lines[-1].end_ms if lines else 0))
    return VideoScript(title=o.title, duration_ms=total, chapters=[], lines=lines, scenes=[])


@router.get("/sessions/{sid}/video/script")
async def get_script(sid: uuid.UUID, s: Db, me: Me, style: StyleQuery = "slides") -> VideoScript:
    """The video's chapters, narration and scenes, with their times."""
    return await script_of(s, await _owned(s, me.id, sid), style)


@router.get("/shares/{share_id}/sessions/{sid}/video/script")
async def shared_script(
    share_id: uuid.UUID, sid: uuid.UUID, s: Db, me: Me, style: StyleQuery = "slides"
) -> VideoScript:
    """The script of a video of an output a share includes."""
    o = await shares.output_of(s, share_id, Session, sid)
    return await script_of(s, o, style)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=8_000)


class ExplainReq(BaseModel):
    style: Style = "whiteboard"
    t_ms: int = Field(ge=0, description="The moment of the video asked about")
    mode: explain.Mode = Field(
        default="ask", description="`ask` answers `question`; the others are the quick prompts"
    )
    question: str = Field(default="", max_length=4_000)
    history: list[Turn] = Field(default_factory=list[Turn], max_length=40)


class ExplainOut(BaseModel):
    answer: str = Field(
        description="Markdown with [n] markers that match the citations, and [m:ss] moments"
    )
    citations: list[Citation]
    t_ms: int = Field(description="The moment answered about")


EMPTY_QUESTION = "The question is empty. Write a question, then ask again."


@router.post("/sessions/{sid}/video/explain")
async def explain_moment(sid: uuid.UUID, body: ExplainReq, s: Db, me: Me) -> ExplainOut:
    """Explain a moment of the video, or answer a question about it, from
    the video's script and the collection's sources. Takes a few seconds."""
    if body.mode == "ask" and not body.question.strip():
        raise Problem(422, EMPTY_QUESTION)
    o = await _owned(s, me.id, sid)
    script = (await script_of(s, o, body.style)).model_dump()
    cid = o.collection_id
    docs = await reading.read_docs(s, me.id, cid, None) if cid else []
    model = await config.value(s, me.id, config.SCRIPT_MODEL_KEY)
    rule = config.language_rule(await config.value(s, me.id, config.LANGUAGE_KEY))
    await release(s)
    try:
        async with ledger.spending(me.id, "explain", collection_id=cid):
            got = await explain.explain(
                docs, script, body.t_ms, body.question, body.mode,
                [(t.role, t.text) for t in body.history], model=model, language_rule=rule,
            )  # fmt: skip
    except (AiError, ScriptError) as e:
        raise problem(e) from e
    return ExplainOut(
        answer=got.text,
        citations=[
            Citation(
                n=c.n,
                name=docs[c.doc].name,
                title=docs[c.doc].title,
                url=docs[c.doc].url,
                excerpt=c.excerpt,
            )
            for c in got.cited
        ],
        t_ms=min(body.t_ms, int(script["duration_ms"])),
    )


# ── themes ───────────────────────────────────────────────────────────────────


class ThemeOut(BaseModel):
    id: str
    label: str
    family: Literal["drawn", "illustrated"] = Field(
        description="`drawn`: the scenes drawn in another paper, ink and hand, at no extra cost; "
        "`illustrated`: a picture per scene, at a cost per scene"
    )


@router.get("/video/themes")
async def video_themes(me: Me) -> list[ThemeOut]:
    """The looks a whiteboard video can be made in, the default first."""
    from opennotebook.build.whiteboard import theme as th

    return [ThemeOut(id=t.id, label=t.label, family=t.family) for t in th.THEMES.values()]


# ── a video overview: an output made for its video ───────────────────────────

# A video overview's length, as the deck it is narrated over: parts, each a
# scene or two of the whiteboard.
LENGTHS = {"short": 4, "default": 6, "long": 9}


class OverviewReq(BaseModel):
    style: Style = "whiteboard"
    length: Literal["short", "default", "long"] = "default"
    title: str = Field(default="", max_length=200)
    theme: ThemeId = Field(default="whiteboard", description="A whiteboard's look")


class OverviewOut(BaseModel):
    session: Any = Field(description="The output being made, as /api/sessions lists it")
    video: VideoState


@router.post("/collections/{cid}/videos", status_code=202)
async def make_overview(cid: uuid.UUID, body: OverviewReq, s: Db, me: Me) -> OverviewOut:
    """Make a video overview of a collection's sources: one narrator's deck,
    built as any deck is (and refused as one would be), then its video once
    it is ready. Follow the build on /api/sessions/{sid}/events and the video
    on /api/sessions/{sid}/videos."""
    from opennotebook.api import sessions as sessions_api

    try:
        video.tool(video.FFMPEG_KEY, "ffmpeg")
        video.tool(video.FFPROBE_KEY, "ffprobe")
    except video.ToolMissing as e:
        raise Problem(503, e.sentence) from e
    # Named as what it is for: without a title a deck is named for its slide
    # style ("Editorial slides"), which says nothing of its video.
    build = sessions_api.BuildReq(
        kind="slides",
        title=body.title.strip() or "Video overview",
        speakers=1,
        slide_count=LENGTHS[body.length],
    )
    made = await sessions_api.build(cid, build, s, me)
    o = await _owned(s, me.id, made.id, lock=True)
    asked: dict[str, Any] = {"state": video.WAITING}
    if body.style == "whiteboard":
        asked["theme"] = body.theme
    o.video = {body.style: asked}
    return OverviewOut(session=made, video=VideoState.of(body.style, asked))


class CollectionVideo(VideoState):
    session_id: uuid.UUID


@router.get("/collections/{cid}/videos")
async def collection_videos(cid: uuid.UUID, s: Db, me: Me) -> list[CollectionVideo]:
    """Every video asked for of a collection's outputs, and how far each is:
    what an outputs list shows on its cards, in one request."""
    rows = await s.scalars(
        select(Session).where(
            Session.owner_id == me.id, Session.collection_id == cid, Session.video.is_not(None)
        )
    )
    out: list[CollectionVideo] = []
    for o in rows:
        for style in video.STYLES:
            if style in (o.video or {}):
                st = await state_of(s, o, style)  # pyright: ignore[reportArgumentType]
                out.append(CollectionVideo(**st.model_dump(), session_id=o.id))
    return out
