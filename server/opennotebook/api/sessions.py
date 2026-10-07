"""Outputs built in the background: narrated slide decks and audio overviews.
Build one, estimate it, follow it, read it, rename, pin, delete, and keep its
playback position."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from opennotebook import jobs, storage
from opennotebook.ai import client
from opennotebook.ai.prices import Price
from opennotebook.api import paging
from opennotebook.api.deps import Db, Me
from opennotebook.auth import current_user
from opennotebook.build import narrate, pipeline, slides, video
from opennotebook.db.models import Job, Playback, Session
from opennotebook.db.session import release, sessionmaker
from opennotebook.domain import collections, reading, sessions, styles, voice
from opennotebook.domain import sessions_estimate as est
from opennotebook.domain import settings as st
from opennotebook.domain.sessions_events import SESSION_CHANNEL_SQL, stream
from opennotebook.errors import Problem, not_found
from opennotebook.jobs.app import PREP_LOCK, PREP_QUEUE
from opennotebook.jobs.tasks import PREP_TASK
from opennotebook.speech import vad

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["outputs"])

Kind = Literal["slides", "audio"]
State = Literal["preparing", "ready", "failed"]


class SessionSummary(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    kind: Kind
    title: str
    display_title: str = Field(
        description="Its name as it is shown: the title, or `Untitled narrated slides` / "
        "`Untitled audio overview` while it has none"
    )
    description: str
    state: State
    failure: str | None = Field(
        description="Why it failed, in a sentence with what to do; null unless failed"
    )
    failure_detail: str | None = Field(
        default=None,
        description="The failure's technical detail, for whoever is debugging; null when "
        "there is none beyond the sentence",
    )
    parts: int = Field(description="Slides of a deck, chapters of an audio overview")
    speakers: int
    audio_format: str = Field(description="An audio overview's format; empty for a deck")
    audio_label: str = Field(
        description="An audio overview's format as a person reads it: `Deep Dive`; empty for a deck"
    )
    duration_ms: int = Field(description="Measured narration length; 0 before it is voiced")
    pinned: bool
    spent_usd: Decimal | None
    spent_known: bool = Field(description="False when a call of the build could not be priced")
    created_at: datetime
    waiting: str | None = Field(
        default=None,
        description="Why a preparing output has not started, in a sentence: its build is "
        "queued and no worker is running. Null otherwise",
    )

    @classmethod
    def of(cls, o: Session, parts: int | None = None) -> SessionSummary:
        """`o` as a list shows it. `parts` is how many it has, when the row
        was read without them (`listed`)."""
        return cls(
            id=o.id,
            collection_id=o.collection_id,
            kind=o.kind,  # pyright: ignore[reportArgumentType]
            title=o.title,
            display_title=sessions.display_title(o.title, o.kind == "audio"),
            description=o.description,
            state=o.state,  # pyright: ignore[reportArgumentType]
            failure=sessions.failure_said(o.failure, None)[0] if o.state == "failed" else None,
            parts=len(o.slides) if parts is None else parts,
            speakers=len(o.speakers),
            audio_format=str((o.audio or {}).get("format", "")),
            audio_label=sessions.FORMAT_LABELS[
                sessions.parse_format(str(o.audio.get("format", "")))
            ]
            if o.audio
            else "",
            duration_ms=o.duration_ms,
            pinned=o.pinned,
            spent_usd=o.spent_usd,
            spent_known=o.spent_known,
            created_at=o.created_at,
        )


class SessionDetail(SessionSummary):
    style: str | None
    speaker_list: list[dict[str, Any]] = Field(description="Speakers with their voices")
    slides: list[dict[str, Any]] = Field(description="The parts with their narration lines")
    audio: dict[str, Any] | None = Field(description="Format, length, focus and minutes")
    speaker_names: dict[str, str] = Field(
        description="What to call each speaker, by speaker id: their own name, or their "
        "voice's when they have only a role word such as `Host`"
    )

    @classmethod
    def full(cls, o: Session) -> SessionDetail:
        named = [sessions.Speaker.of_json(sp) for sp in o.speakers]
        return cls(
            **SessionSummary.of(o).model_dump(),
            speaker_names={
                sp.speaker_id: sessions.display_name(sp.display_name, sp.voice_id) or sp.speaker_id
                for sp in named
            },
            style=o.style,
            speaker_list=o.speakers,
            slides=o.slides,
            audio=o.audio,
        )


class BuildReq(BaseModel):
    kind: Kind
    title: str = Field(default="", max_length=200)
    speakers: int | None = Field(default=None, ge=1, le=2)
    slide_count: int | None = Field(default=None, description="3 to 12; outside is clamped")
    style: str | None = Field(default=None, description="A style id from /api/styles")
    audio_format: Literal["deep_dive", "brief", "critique", "debate"] | None = None
    audio_length: Literal["shorter", "default", "longer"] | None = None
    focus: str = Field(default="", max_length=400)
    research: str = Field(
        default="",
        max_length=400,
        description="A topic to research on the web first; the report is added as a source. "
        "Empty builds from the sources as they are",
    )


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None


class Playhead(BaseModel):
    slide_ordinal: int = 0
    line_id: str = Field(default="", description="Empty before the first line starts")
    offset_ms: int = 0
    state: Literal["idle", "playing", "paused", "finished"] = "idle"


class CostLine(BaseModel):
    """One step of a build and what it costs: low, typical and high."""

    group: str = Field(description="The stage it belongs to: Reading your sources, …")
    step: str
    detail: str
    model: str = Field(description="Empty for a step that calls no model")
    via: str = Field(description="The service that makes the call and how it is paid")
    calls_low: int
    calls_typical: int
    calls_high: int
    input_tokens: int
    output_tokens_low: int
    output_tokens_typical: int
    output_tokens_high: int
    cost_low_usd: float
    cost_typical_usd: float
    cost_high_usd: float
    free: bool
    unpriced: bool = Field(description="The model has no price in the catalogue: counted as $0")
    price_in_per_million: float | None
    price_out_per_million: float | None

    @classmethod
    def of(cls, ln: est.Line) -> CostLine:
        p = ln.price
        return cls(
            group=ln.group,
            step=ln.step,
            detail=ln.detail,
            model=ln.model,
            via=ln.via,
            calls_low=ln.calls.low,
            calls_typical=ln.calls.typical,
            calls_high=ln.calls.high,
            input_tokens=ln.input_tokens,
            output_tokens_low=ln.output_tokens.low,
            output_tokens_typical=ln.output_tokens.typical,
            output_tokens_high=ln.output_tokens.high,
            cost_low_usd=ln.cost[0],
            cost_typical_usd=ln.cost[1],
            cost_high_usd=ln.cost[2],
            free=ln.free,
            unpriced=ln.unpriced,
            price_in_per_million=None if p is None else p.input_per_token * 1_000_000,
            price_out_per_million=None if p is None else p.output_per_token * 1_000_000,
        )


class SessionEstimate(BaseModel):
    """What a build would cost, step by step: every input measured, every
    output a low / typical / high range."""

    total_low_usd: float
    total_typical_usd: float
    total_high_usd: float
    lines: list[CostLine]
    assumptions: list[str]
    sources: int
    source_chars: int
    slides: int = Field(description="Slides, or an audio overview's chapters")
    speakers: int
    style: str
    slides_tier: str = Field(description="The model that writes the slides")
    priced_at: str = Field(description="When the prices were read, RFC 3339")
    minutes: int
    limit_usd: float = Field(description="The spending limit; 0 is none")
    over_limit: bool = Field(description="The high estimate is over the limit: a build is refused")
    limit_note: str | None = Field(
        default=None,
        description="Over the limit: why it would be refused and what to change, ending with "
        "where the limit is set (`Settings › Costs & limits.`), as a refusal says it. Null "
        "when it is within the limit",
    )
    model: str = Field(
        description="The model that does most of the writing: the script's, or the one that "
        "makes a mind map or study notes"
    )
    facts: list[str] = Field(
        description="What it is made of, in words, as the cost dialog lists them: "
        "`5 slides`, `about 5 minutes`, `2 voices`, `3 sources · 12.4k characters`…"
    )

    @classmethod
    def of(cls, live: est.Live) -> SessionEstimate:
        e, i = live.estimate, live.inputs
        facts = [live.audio.said() if live.audio else f"{i.slides} slides"]
        if i.minutes > 0:
            facts.append(f"about {i.minutes} minutes")
        facts.append("1 voice" if i.speakers == 1 else f"{i.speakers} voices")
        chars = est.count_short(e.source_chars)
        facts.append(f"{est.sources_said(len(i.source_chars))} · {chars} characters")
        if not i.audio:
            found = styles.style(live.style)
            facts.append(f"{found.label if found else live.style} style")
            facts.append(f"slides by {est.model_name(i.slide_model)}")
        if live.video is not None:
            facts.append(f"{live.video.style.capitalize()} video")
            if live.video.theme:
                facts.append(f"{live.video.theme} theme")
        return cls(
            total_low_usd=e.total[0],
            total_typical_usd=e.total[1],
            total_high_usd=e.total[2],
            lines=[CostLine.of(ln) for ln in e.lines],
            assumptions=e.assumptions,
            sources=len(i.source_chars),
            source_chars=e.source_chars,
            slides=i.slides,
            speakers=i.speakers,
            style=live.style,
            slides_tier=i.slide_model,
            priced_at=live.priced_at,
            minutes=i.minutes,
            limit_usd=live.limit_usd,
            over_limit=live.over_limit,
            limit_note=est.over_limit_message(e.total[2], live.limit_usd, live.fix)
            if live.over_limit
            else None,
            model=i.script_model,
            facts=facts,
        )

    @classmethod
    def one_call(
        cls, q: reading.Estimate, price: Price | None, group: str, step: str
    ) -> SessionEstimate:
        """A mind map's or study notes' estimate in a build's itemised shape,
        so every tool is said and checked the same way: one step, one model,
        one call, and a second when the first answer comes back too thin."""
        twice = q.cost_high_usd > q.cost_usd
        line = CostLine(
            group=group,
            step=step,
            detail="Reads every source whole in one call; a second call when the first answer "
            "comes back too thin.",
            model=q.model,
            via=est.SCRIPT_VIA,
            calls_low=1,
            calls_typical=1,
            calls_high=2 if twice else 1,
            input_tokens=q.input_tokens,
            output_tokens_low=q.output_tokens,
            output_tokens_typical=q.output_tokens,
            output_tokens_high=2 * q.output_tokens if twice else q.output_tokens,
            cost_low_usd=q.cost_usd,
            cost_typical_usd=q.cost_usd,
            cost_high_usd=q.cost_high_usd,
            free=False,
            unpriced=not q.priced,
            price_in_per_million=None if price is None else price.input_per_token * 1_000_000,
            price_out_per_million=None if price is None else price.output_per_token * 1_000_000,
        )
        assumptions = [
            f"Measured: {est.sources_said(q.sources)} ({est.grouped(q.chars)} characters), "
            "today's prices.",
            "Tokens are counted at about four characters each, the prompt included.",
            "Prices are the model's list prices on the AI endpoint.",
        ]
        if not q.priced:
            assumptions.append(
                'A model with no price in the catalog is counted as $0 and marked "no price"; '
                "the real cost is higher."
            )
        return cls(
            total_low_usd=q.cost_usd,
            total_typical_usd=q.cost_usd,
            total_high_usd=q.cost_high_usd,
            lines=[line],
            assumptions=assumptions,
            sources=q.sources,
            source_chars=q.chars,
            slides=0,
            speakers=0,
            style="",
            slides_tier="",
            priced_at=est.stamp_now(),
            minutes=0,
            limit_usd=q.limit_usd,
            over_limit=q.over_limit,
            limit_note=est.over_limit_message(q.cost_high_usd, q.limit_usd, est.FEWER_SOURCES)
            if q.over_limit
            else None,
            model=q.model,
            facts=[
                f"{est.sources_said(q.sources)} · {est.grouped(q.chars)} characters",
                f"by {est.model_name(q.model)}",
            ],
        )


class JobOut(BaseModel):
    """A piece of background work and how far it got."""

    id: uuid.UUID
    kind: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    step: str = Field(description="What it is doing now; empty before it starts")
    steps_done: int
    steps_total: int = Field(description="0 when the work does not report its steps")
    error: str | None = Field(
        description="Why it failed or stopped, always said when it did; null otherwise"
    )
    session_id: uuid.UUID | None
    collection_id: uuid.UUID | None
    created_at: datetime
    waiting: str | None = Field(
        default=None,
        description="Why queued work has not started, in a sentence: no worker is running. "
        "Null otherwise",
    )

    @classmethod
    def of(cls, j: Job) -> JobOut:
        out = cls.model_validate(j, from_attributes=True)
        # Work stopped from outside, or before it could say why, still says
        # something a person can act on.
        if out.status in ("failed", "cancelled") and not out.error:
            out.error = jobs.STOPPED
        return out


# ── reading ──────────────────────────────────────────────────────────────────


def listed() -> Select[Session, int]:
    """Outputs as a list shows them, each with how many parts it has: its
    parts themselves, every narration line, are left in the database."""
    return select(Session, func.jsonb_array_length(Session.slides)).options(
        defer(Session.slides, raiseload=True)
    )


async def summaries_of(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> list[SessionSummary]:
    return (await summaries_and_jobs(s, owner, cid))[0]


async def summaries_and_jobs(
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID
) -> tuple[list[SessionSummary], dict[uuid.UUID, Job]]:
    """A collection's outputs as its page lists them, newest first, with the
    job of each one still being made."""
    rows = await s.execute(
        listed()
        .where(Session.owner_id == owner, Session.collection_id == cid)
        .order_by(Session.created_at.desc())
    )
    return await _summaries(s, list(rows))


async def _summaries(
    s: AsyncSession, rows: list[tuple[Session, int]]
) -> tuple[list[SessionSummary], dict[uuid.UUID, Job]]:
    """Outputs as a list shows them, each reconciled, saying why it waits,
    and why it failed with its detail; with the job of each one still being
    made. The jobs of all of them are read together, and whether a worker is
    running at most once, however many there are."""
    asked = [o.id for o, _ in rows if o.state in ("preparing", "failed")]
    statuses = await sessions.job_statuses(s, asked)
    alive: bool | None = None
    out: list[SessionSummary] = []
    making: dict[uuid.UUID, Job] = {}
    for o, parts in rows:
        job, status = statuses.get(o.id, (None, "inactive"))
        o = await sessions.reconcile(s, o, status)
        one = SessionSummary.of(o, parts)
        if o.state == "preparing":
            if job is not None:
                making[o.id] = job
            if status == "queued":
                if alive is None:
                    alive = await sessions.worker_alive(s)
                one.waiting = None if alive else sessions.WAITING
        elif o.state == "failed":
            one.failure, one.failure_detail = sessions.failure_said(
                o.failure, job.error if job is not None else None
            )
        out.append(one)
    return out, making


async def _failure(s: AsyncSession, o: Session, out: SessionSummary) -> None:
    """A failed output's reason and, apart from it, the technical detail its
    build's job recorded."""
    if o.state != "failed":
        return
    job, _ = await sessions.job_status(s, o.id)
    out.failure, out.failure_detail = sessions.failure_said(
        o.failure, job.error if job is not None else None
    )


async def _one(s: AsyncSession, owner: uuid.UUID, sid: uuid.UUID) -> Session:
    o = await s.scalar(select(Session).where(Session.id == sid, Session.owner_id == owner))
    if o is None:
        raise not_found("That output")
    return o


# ── building ─────────────────────────────────────────────────────────────────

NO_SOURCES = "Add a source first: a link, a note, or a topic to research."
NO_KEY = (
    "The studio has no AI key yet. Add OPENNOTEBOOK_AI_KEY to the server's environment, "
    "then try again."
)


async def _planned(
    s: AsyncSession, owner: uuid.UUID, body: BuildReq, sources: int
) -> sessions.Planned:
    """The build the request asks for, with the person's settings filling
    what it leaves out: the one plan the estimate prices and the build
    makes."""
    d = await sessions.BuildDefaults.read(s, owner)
    audio_format = (body.audio_format or d.audio_format) if body.kind == "audio" else None
    try:
        return sessions.plan(
            sessions.Ask(
                speakers=body.speakers,
                slide_count=body.slide_count,
                style=body.style,
                audio_format=audio_format,
                audio_length=body.audio_length,
                focus=body.focus,
                sources=sources,
            ),
            d,
        )
    except sessions.PlanError as e:
        raise Problem(422, str(e)) from e


@router.post("/collections/{cid}/outputs", status_code=202)
async def build(cid: uuid.UUID, body: BuildReq, s: Db, me: Me) -> SessionSummary:
    """Build a narrated slide deck or an audio overview from a collection's
    sources, with the person's settings for what the request leaves out.
    Takes minutes; follow it on /api/sessions/{sid}/events. Refused when its
    high estimate is over the spending limit."""
    return SessionSummary.of(await start(s, me.id, cid, body))


async def start(
    s: AsyncSession,
    owner: uuid.UUID,
    cid: uuid.UUID,
    body: BuildReq,
    video: est.Render | None = None,
) -> Session:
    """Check a build, write its row as `preparing` and queue its job: the
    one way an output starts, new or tried again. Nothing is written when it
    is refused. A video overview's deck is checked against the limit with
    the video's render (`video`)."""
    await collections.editable(s, owner, cid)
    if not client.ai().has_key:
        # Refused at once rather than queued: a build with no key fails at
        # its first call, minutes later, after waiting its turn.
        raise Problem(503, NO_KEY)
    # The price list, which can take seconds to read, is read before the
    # lock is taken and with no transaction open. Only with a limit to check.
    prices = None
    if st.parse_limit(await st.value(s, owner, st.MAX_BUILD_USD_KEY)) is not None:
        await release(s)
        prices = await client.ai().catalogue.prices()
    # Under the collection's lock: a build never lands in a collection being
    # deleted, and a deleted output is never written back.
    await collections.lock(s, owner, cid)
    chars = await est.source_chars(s, owner, cid)
    research = body.research.strip()
    if not chars and not research:
        raise Problem(422, NO_SOURCES)
    planned = await _planned(s, owner, body, len(chars))
    # Before anything is written or queued: a refused build leaves no row
    # and no job behind, only the reason. Priced on the shape the pipeline
    # will build, so an audio overview is checked on its chapters and its
    # format's voices.
    if prices is not None and (
        why := await est.refuse_over_limit(
            s, owner, chars, planned.shape(), bool(research), prices=prices, video=video
        )
    ):
        raise Problem(422, why)
    audio = planned.audio
    style = None if audio else planned.style
    speakers = [sp.named() for sp in planned.speakers][: planned.shape().speakers]
    # The row goes in before the job, as `preparing`: a refresh or a restart
    # while the build waits its turn still finds it, with its progress.
    o = Session(
        owner_id=owner,
        collection_id=cid,
        kind="audio" if audio else "slides",
        title=sessions.output_title(body.title, style, audio),
        state="preparing",
        style=style,
        speakers=[sp.as_json() for sp in speakers],
        slides=[],
        audio=audio.as_json() if audio else None,
    )
    s.add(o)
    await s.flush()
    await s.refresh(o)
    job = await jobs.create(
        s, owner, "prep", collection_id=cid, session_id=o.id, steps_total=len(pipeline.PHASES)
    )
    spec = pipeline.PrepSpec(
        job_id=job.id,
        session_id=o.id,
        owner_id=owner,
        collection_id=cid,
        speakers=[sp.as_json() for sp in speakers],
        slide_count=planned.slide_count,
        style=planned.style,
        audio_format=audio.format if audio else None,
        audio_length=audio.real_length() if audio else None,
        focus=audio.focus if audio else None,
        research_topic=research or None,
    )
    await jobs.defer(
        s, job, PREP_TASK, spec.model_dump(mode="json"), queue=PREP_QUEUE, lock=PREP_LOCK
    )
    await collections.touch(s, cid)
    return o


@router.post("/collections/{cid}/outputs/estimate")
async def estimate(cid: uuid.UUID, body: BuildReq, s: Db, me: Me) -> SessionEstimate:
    """What a build with the same arguments would cost, step by step, before
    running it. The same plan as the build, so the estimate describes exactly
    the build the button next to it starts. Makes no model call."""
    return await priced(s, me.id, cid, body)


async def priced(
    s: AsyncSession,
    owner: uuid.UUID,
    cid: uuid.UUID,
    body: BuildReq,
    video: est.Render | None = None,
) -> SessionEstimate:
    """A build's estimate, on the plan the build would make, and with a video
    overview's render (`video`) when it is one."""
    await collections.editable(s, owner, cid)
    chars = await est.source_chars(s, owner, cid)
    research = body.research.strip()
    if not chars and not research:
        raise Problem(422, NO_SOURCES)
    planned = await _planned(s, owner, body, len(chars))
    # The price list can take seconds to read; no transaction waits for it.
    await release(s)
    try:
        live = await est.compute(
            s,
            owner,
            chars,
            planned.shape(),
            "" if planned.audio else planned.style,
            bool(research),
            video=video,
        )
    except est.NoPrices as e:
        raise Problem(503, e.sentence) from e
    return SessionEstimate.of(live)


# How many outputs a page of the list holds unless asked for fewer, and at
# most.
LIST_DEFAULT = 200
LIST_MAX = 500


@router.get("/sessions")
async def list_sessions(
    s: Db,
    me: Me,
    response: Response,
    limit: Annotated[int, paging.limit(LIST_DEFAULT, LIST_MAX, "outputs")] = LIST_DEFAULT,
    offset: paging.Offset = 0,
) -> list[SessionSummary]:
    """Your decks and audio overviews, newest first, a page at a time:
    `X-Next-Offset` says where the next page starts when there is one."""
    rows = await s.execute(
        listed()
        .where(Session.owner_id == me.id)
        .order_by(Session.created_at.desc(), Session.id.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    return (await _summaries(s, paging.cut(list(rows), limit, offset, response)))[0]


@router.get("/sessions/{sid}")
async def get_session(sid: uuid.UUID, s: Db, me: Me) -> SessionDetail:
    """One output in full: its state, parts and every narration line."""
    o = await sessions.reconcile(s, await _one(s, me.id, sid))
    out = SessionDetail.full(o)
    out.waiting = await sessions.waiting(s, o)
    await _failure(s, o, out)
    return out


@router.patch("/sessions/{sid}")
async def update_session(sid: uuid.UUID, body: SessionPatch, s: Db, me: Me) -> SessionSummary:
    """Rename or pin an output. One in a read-only copy can be pinned, not
    renamed."""
    o = await _one(s, me.id, sid)
    c = await collections.lock(s, me.id, o.collection_id)
    if body.title is not None:
        await collections.refuse_read_only(s, c)
        o.title = " ".join(body.title.split())
    if body.pinned is not None:
        o.pinned = body.pinned
    return SessionSummary.of(o)


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(sid: uuid.UUID, s: Db, me: Me, after: BackgroundTasks) -> None:
    """Delete an output. A build still running is stopped first; its files go
    once the row is gone for good."""
    o = await _one(s, me.id, sid)
    await collections.refuse_read_only(s, await collections.lock(s, me.id, o.collection_id))
    await jobs.stop(s, sid)
    await s.execute(delete(Session).where(Session.id == sid, Session.owner_id == me.id))
    await collections.touch(s, o.collection_id)
    after.add_task(storage.remove_tree, narrate.audio_dir(sid))
    after.add_task(storage.remove_tree, slides.deck_dir(sid))
    after.add_task(storage.remove_tree, video.video_dir(sid))


NOT_FAILED = "Only an output that failed can be tried again. Reload the page to see where it is."


def retry_request(o: Session) -> BuildReq:
    """What a failed output was asked to be, read back from its row: its
    title, kind, style, slide count, audio format, length and focus, and its
    own number of voices. Voices are not read again from the settings, so a
    change there between the failure and the retry does not change what it
    sounds like. What the row does not record (an older one has no style; a
    build that failed before its outline has no slides) is left out, and the
    settings fill it as they do for a new build."""
    speakers = len(o.speakers) if len(o.speakers) in (1, 2) else None
    audio = sessions.AudioSpec.of_json(o.audio)
    if audio is not None:
        return BuildReq(
            kind="audio",
            title=o.title,
            speakers=speakers,
            audio_format=audio.format,  # pyright: ignore[reportArgumentType]
            audio_length=audio.real_length(),  # pyright: ignore[reportArgumentType]
            focus=audio.focus,
        )
    # A style since retired is left to the settings rather than refused.
    style = o.style if o.style and styles.style(o.style) is not None else None
    return BuildReq(
        kind="slides",
        title=o.title,
        speakers=speakers,
        slide_count=len(o.slides) or None,
        style=style,
    )


@router.post("/sessions/{sid}/retry", status_code=202)
async def retry_session(sid: uuid.UUID, s: Db, me: Me, after: BackgroundTasks) -> SessionSummary:
    """Make a failed output again, with the options it was made with, as a
    new output in the same collection. One step: the new one is started and
    the failed one removed together, or, when the new one is refused (no
    sources any more, over the spending limit, no AI key), neither, and the
    failed one stays with its reason."""
    old = await sessions.reconcile(s, await _one(s, me.id, sid))
    if old.state != "failed":
        raise Problem(409, NOT_FAILED)
    o = await start(s, me.id, old.collection_id, retry_request(old))
    # Read again under the collection's lock `start` holds: another tab may
    # have removed or retried it while the price list was read.
    gone = await s.execute(
        delete(Session).where(
            Session.id == sid, Session.owner_id == me.id, Session.state == "failed"
        )
    )
    if not gone.rowcount:  # pyright: ignore[reportAttributeAccessIssue]
        raise Problem(409, NOT_FAILED)
    after.add_task(storage.remove_tree, narrate.audio_dir(sid))
    after.add_task(storage.remove_tree, slides.deck_dir(sid))
    return SessionSummary.of(o)


@router.get("/sessions/{sid}/playback")
async def get_playback(sid: uuid.UUID, s: Db, me: Me) -> Playhead:
    """Where the output's playback is."""
    await _one(s, me.id, sid)
    p = await s.get(Playback, sid)
    return Playhead() if p is None else Playhead.model_validate(p, from_attributes=True)


@router.put("/sessions/{sid}/playback")
async def set_playback(sid: uuid.UUID, body: Playhead, s: Db, me: Me) -> Playhead:
    """Record where playback is: started, paused, moving, or finished."""
    await _one(s, me.id, sid)
    values = body.model_dump()
    await s.execute(
        insert(Playback)
        .values(session_id=sid, owner_id=me.id, **values)
        .on_conflict_do_update(index_elements=[Playback.session_id], set_=values)
    )
    # Whoever follows the output's events hears of it once this commits.
    await s.execute(text(SESSION_CHANNEL_SQL), {"sid": str(sid)})
    return body


def _frame(name: str, data: dict[str, Any]) -> str:
    # The name is the `event:` field so a browser can use
    # `addEventListener("line.start", …)` rather than parsing a
    # discriminator out of the payload.
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


async def _frames(owner: uuid.UUID, sid: uuid.UUID) -> AsyncIterator[str]:
    async for e in stream(owner, sid):
        # A comment frame when nothing changed: proxies close a silent
        # connection.
        yield ": keepalive\n\n" if e is None else _frame(*e)


@router.get(
    "/sessions/{sid}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}},
)
async def session_events(sid: uuid.UUID, request: Request) -> StreamingResponse:
    """Server-sent events while an output is made and played:
    `session.state`, `prep.progress` (step, label, steps_done, steps_total), and
    the playback events `slide.enter`, `line.start`, `line.end`, `playhead`.
    The current state comes first, so a client that connects late misses
    nothing."""
    # Signed in and checked on a session of its own, closed before the
    # stream starts: a stream lasts minutes, and holds no transaction.
    async with sessionmaker()() as s, s.begin():
        me = await current_user(request, s)
        await _one(s, me.id, sid)
    return StreamingResponse(
        _frames(me.id, sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, s: Db, me: Me) -> JobOut:
    """A piece of background work, such as deep research, and how far it
    got."""
    j = await s.scalar(select(Job).where(Job.id == job_id, Job.owner_id == me.id))
    if j is None:
        raise not_found("That piece of work")
    out = JobOut.of(j)
    if j.status == jobs.QUEUED and not await sessions.worker_alive(s):
        out.waiting = sessions.WAITING
    return out


def _sse(frames: AsyncIterator[voice.Frame]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        async for name, data in frames:
            yield f"event: {name}\ndata: {data}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _once(*frames: voice.Frame) -> AsyncIterator[voice.Frame]:
    for f in frames:
        yield f


@router.post(
    "/sessions/{sid}/voice",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The question, recorded: a WAV, 16-bit PCM mono at any rate",
            "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def voice_ask(
    sid: uuid.UUID,
    request: Request,
    slide: int = Query(default=0, description="The part the playhead was on"),
    line: str = Query(default="", description="The line under the playhead; empty before one"),
    offset_ms: int = Query(default=0, description="How far into that line it was"),
) -> StreamingResponse:
    """Ask a question aloud while listening to one of your outputs. The
    answer comes back as server-sent events in the voice of whoever was
    talking: `speaker`, then `said` with its `audio` (base64 PCM16, 24 kHz
    mono), `hold`/`hold_audio` while a slow answer is prepared, `heard` (what
    the question was heard as) or `heard_failed`, and `done`. A recording
    with no speech in it is answered `silent` and costs nothing; a question
    that cannot be answered is a `failed` with the reason."""
    # Signed in and checked on a session of its own, closed before the
    # stream starts: the stream lasts as long as the answer, and holds no
    # transaction. Yours only: a share's viewer is not charged for a model
    # call on someone else's output, and the player says so before asking.
    async with sessionmaker()() as s, s.begin():
        me = await current_user(request, s)
        o = await _one(s, me.id, sid)
        values = await st.values(s, me.id)
    wav = bytearray()
    async for chunk in request.stream():
        wav += chunk
        if len(wav) > voice.MAX_UPLOAD_BYTES:
            return _sse(_once(voice.failed(voice.TOO_LONG)))
    if not wav:
        return _sse(_once(voice.failed(voice.NO_AUDIO)))
    # The silence gate, in front of the model call, so silence is never
    # billed. The transcript cannot be that gate: speech-to-text returns
    # `{"text":""}` byte for byte for silence, for noise and for speech it
    # could not resolve, and it arrives in parallel with the answer, so by
    # the time it could tell, the answer model has been called and billed.
    # This runs on the same bytes the model would have received, before it
    # receives them.
    try:
        a = await asyncio.to_thread(vad.analyze_wav, bytes(wav))
    except (vad.NotAudio, vad.DetectorMissing) as e:
        if isinstance(e, vad.DetectorMissing):
            log.error("voice: %s", e)
        return _sse(_once(voice.failed(e.sentence)))
    if a.is_silent:
        return _sse(_once(voice.silent(a)))
    if not client.ai().has_key:
        return _sse(_once(voice.failed(voice.NO_KEY)))
    at = voice.At(slide=slide, line=line, offset_ms=offset_ms)
    return _sse(voice.turn(voice.prepare(o, values, at, bytes(wav), a.speech_ms)))
