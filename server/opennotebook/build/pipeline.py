"""The prep pipeline: a collection's sources in, a `ready` deck or audio
overview out. A port of `opennotebook_server/src/pipeline.rs` and
`opennotebook_build/src/prep.rs`.

```text
1. research (when asked)    minutes           the report becomes a source
2. ingest                   seconds           search index, Q&A pairs
3. outline + script         a few LLM calls   script.generate
4. slides + narration       4 slides a call, local voice, at the same time
5. validation               liveness only
6. state -> ready           once
```

Partial failure is not rolled back. A half-imported source is invisible; an
output is the opposite. It carries `state`, so an incomplete one can say so,
and what it holds is expensive: a model call per four slides and seconds of
synthesis per line, all of which a rollback would throw away.

So the rule here is narrower and it is the whole guarantee: `ready` is
written once, after validation, and on no other path. Everything else is
written as `preparing` or `failed`. The row is written before the slides are
drawn and again after, so work that dies mid-prep leaves a record of what
exists rather than files nothing points at.

Every row write asks first whether the output is still there (the row is
read under its lock), so one deleted during the minutes below stays deleted:
nothing is written back, and the build stops as abandoned.
"""

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import errors, memory, research, speech
from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.build import kits, narrate, slides, validate, video
from opennotebook.build.errors import Abandoned, BuildError
from opennotebook.build.phases import PHASES
from opennotebook.db.models import Chunk, QaPair, Session, Source
from opennotebook.db.session import sessionmaker
from opennotebook.domain import collections, styles
from opennotebook.domain import settings as st
from opennotebook.domain.sessions import (
    AudioSpec,
    Part,
    Speaker,
    audio_spec,
    duration_of,
    shape,
)
from opennotebook.jobs import NoJob, Progress
from opennotebook.memory import qa
from opennotebook.script import budget
from opennotebook.script.errors import ScriptError
from opennotebook.script.generate import ScriptSpec, generate_script
from opennotebook.script.retrieval import Scope

log = logging.getLogger(__name__)


class PrepSpec(BaseModel):
    """What a build makes, as the queue carries it. Decoded strictly: a spec
    that silently became empty would prepare an empty output and report
    success."""

    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    session_id: uuid.UUID
    owner_id: uuid.UUID
    collection_id: uuid.UUID
    speakers: list[dict[str, str]] = Field(min_length=1)
    slide_count: int
    style: str = ""
    audio_format: str | None = None
    audio_length: str | None = None
    focus: str | None = None
    research_topic: str | None = None

    def audio(self) -> AudioSpec | None:
        return audio_spec(self.audio_format, self.audio_length, self.focus)


class NoSources(BuildError):
    sentence = "Add a source first: a link, a note, or a topic to research."

    def __init__(self) -> None:
        super().__init__(
            "no sources to build from: nothing was added and web research found nothing, and an "
            "output with no source material would be narrated from nothing"
        )


def sentence_of(e: BaseException) -> str:
    """What a failed build says to the person who started it."""
    if isinstance(e, AiError | ScriptError | BuildError | research.ResearchError):
        return e.sentence
    if isinstance(e, speech.SpeechError):
        return e.sentence
    return errors.SERVER_FAULT


# ── the row ──────────────────────────────────────────────────────────────────


async def _locked(s: AsyncSession, sid: uuid.UUID) -> Session:
    """The output's row under its lock, or `Abandoned` when it is gone: a
    row written back now would undelete it."""
    row = await s.scalar(
        select(Session)
        .where(Session.id == sid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise Abandoned(sid)
    return row


async def persist(sid: uuid.UUID, spend: ledger.Spend | None = None, **fields: Any) -> Session:
    """Write fields onto the output's row, only while it is there. What the
    build has spent so far goes with every write, so a failed build shows
    what it cost too."""
    async with sessionmaker()() as s, s.begin():
        row = await _locked(s, sid)
        for k, v in fields.items():
            setattr(row, k, v)
        if "slides" in fields:
            row.duration_ms = duration_of([Part.of_json(p) for p in row.slides])
        if spend is not None:
            row.spent_usd = spend.total_usd.quantize(Decimal("0.000001"))
            row.spent_known = spend.known
        row.updated_at = func.now()
        if fields.get("state") in ("ready", "failed"):
            await collections.touch(s, row.collection_id)
        return row


# ── the phases ───────────────────────────────────────────────────────────────


async def add_research(owner: uuid.UUID, cid: uuid.UUID, found: research.Found) -> Source:
    """The report, kept as one more source of the collection, searchable at
    once. Embedded first, then written in a short transaction."""
    from opennotebook.domain import sources

    ready = await sources.prepare("research", found.title, found.text)
    async with sessionmaker()() as s, s.begin():
        return await sources.keep(s, owner, cid, ready)


async def ingest(owner: uuid.UUID, cid: uuid.UUID) -> int:
    """Make every source searchable and give it its questions and answers:
    a source added before indexing existed is indexed now, and one without
    pairs is read for them. A source that already has both costs nothing.
    Returns how many sources the build reads."""
    async with sessionmaker()() as s:
        rows = list(
            await s.execute(
                select(
                    Source,
                    exists().where(Chunk.source_id == Source.id),
                    exists().where(QaPair.source_id == Source.id),
                )
                .where(Source.owner_id == owner, Source.collection_id == cid)
                .order_by(Source.created_at)
            )
        )
    if not rows:
        raise NoSources
    for src, indexed, paired in rows:
        if not indexed:
            # Embedded outside any transaction: it is a model call.
            passages = await memory.passages(src.text)
            async with sessionmaker()() as s, s.begin():
                if await s.get(Source, src.id) is not None:
                    await memory.store(s, src, passages)
        if not paired:
            # The model calls run outside any transaction: they take minutes.
            found = await qa.extract(qa.qa_model(), src.name, src.text, qa.DIMENSIONS)
            async with sessionmaker()() as s, s.begin():
                if await s.get(Source, src.id) is not None:
                    await qa.store(s, src, found)
    return len(rows)


async def run(spec: PrepSpec) -> None:
    """Run one prep to completion: the output ends `ready` or `failed`, or,
    deleted meanwhile, untouched."""
    progress = Progress(spec.job_id, len(PHASES))
    async with ledger.spending(
        spec.owner_id,
        "build",
        collection_id=spec.collection_id,
        session_id=spec.session_id,
        job_id=spec.job_id,
    ) as spend:
        try:
            await progress.start()
            await _run(spec, progress, spend)
        except NoJob, Abandoned:
            # Nobody wants this output any more: what it was being made for
            # was deleted while it was waiting or being made. Not a failure
            # to record: there is no row to record it on, and writing one
            # would undo the delete.
            log.info("output %s was abandoned; nothing was written", spec.session_id)
            return
        except asyncio.CancelledError:
            # Stopped: by a delete, or by whoever runs the studio. The row is
            # left to the stop, which deleted it or will be reconciled.
            await asyncio.shield(progress.cancelled())
            raise
        except Exception as e:
            if not isinstance(e, AiError | ScriptError | BuildError | research.ResearchError):
                log.exception("output %s failed", spec.session_id)
            else:
                log.info("output %s failed: %s", spec.session_id, e)
            try:
                await persist(spec.session_id, spend, state="failed", failure=sentence_of(e))
            except Abandoned:
                return
            await progress.finish(str(e) or type(e).__name__)
            return
    await progress.finish(None)
    # A video asked for together with this output starts now that it is ready.
    await video.after_build(spec.session_id)


async def _run(spec: PrepSpec, progress: Progress, spend: ledger.Spend) -> None:
    sid, owner, cid = spec.session_id, spec.owner_id, spec.collection_id
    # An output deleted between the click and this starting — a prep queued
    # behind another waits for its turn — or one whose collection was, has
    # nothing left to build into. Asked before research, which would spend a
    # minute on a collection that is gone.
    row = await persist(sid)
    async with sessionmaker()() as s:
        values = await st.values(s, owner)

    # Web research first, so what it finds is read like any other source.
    await progress.phase(PHASES[0])
    topic = (spec.research_topic or "").strip()
    if topic:
        spend.kind = "research"
        try:
            await add_research(owner, cid, await research.gather(values, topic))
        except research.ResearchError as e:
            # Not fatal while the person gave us something of their own: the
            # output is built from that and the log says what was missing.
            # With nothing of theirs it is — see ingest.
            log.info("web research did not arrive: %s", e)
    await progress.phase_done(PHASES[0])

    await progress.phase(PHASES[1])
    spend.kind = "qa_extract"
    await ingest(owner, cid)
    await progress.phase_done(PHASES[1])

    # The parts and voices, by the same rule the spending limit priced: an
    # audio overview's format decides its chapters and voices (Brief is one
    # host).
    audio = spec.audio()
    sh = shape(spec.slide_count, len(spec.speakers), audio)
    speakers = [Speaker.of_json(x).named() for x in spec.speakers][: sh.speakers]
    # Read once, here, so the script and the deck are made under the same
    # settings even if someone changes them while this runs. An audio
    # overview's length is its format's, not the session setting.
    minutes = audio.minutes() if audio else st.session_minutes(values[st.SESSION_MINUTES_KEY])
    script = ScriptSpec(
        title=row.title,
        speakers=speakers,
        deck_collection=str(sid),
        deck_presentation=slides.PRESENTATION,
        slide_count=sh.slides,
        slide_narration=budget.slide_narration(minutes, sh.slides),
        # No `[image: …]` lines: there is no image model. The slide writer
        # draws each slide's figure itself, from the slide's copy.
        images_per_slide=0,
        audio=audio,
        language=values[st.LANGUAGE_KEY],
        model=values[st.SCRIPT_MODEL_KEY],
    )
    style = styles.style(spec.style) or styles.DEFAULT_STYLE
    kit = kits.kit(style.id) or kits.kit(styles.DEFAULT_STYLE.id)
    assert kit is not None, "the default style has a kit"
    log.info(
        "output %s: %d minutes over %d parts (%d characters a part), %s style, slides on %s",
        sid,
        minutes,
        script.slide_count,
        script.slide_narration,
        style.id,
        values[st.SLIDE_MODEL_KEY],
    )
    await progress.phase(PHASES[2])
    spend.kind = "script"
    parts = await generate_script(Scope(owner, cid), script)
    await progress.phase_done(PHASES[2])
    await persist(
        sid,
        spend,
        speakers=[x.as_json() for x in speakers],
        slides=[p.as_json() for p in parts],
        state="preparing",
    )

    # Slides and audio together: they need nothing from each other, so a
    # build takes as long as the slower of the two rather than both.
    await progress.phase(PHASES[3])
    spend.kind = "slides"
    voice = speech.speech()
    fields: dict[str, Any] = {}
    if audio is not None:
        await narrate.synthesise_all(voice, sid, speakers, parts)
        log.info("output %s narrated: %d chapters", sid, len(parts))
    else:
        plan = slides.DeckPlan(
            title=row.title,
            kit=kit,
            style_label=style.label,
            style_brief=style.brief,
            model=values[st.SLIDE_MODEL_KEY],
            language_rule=st.language_rule(values[st.LANGUAGE_KEY]),
            sid=str(sid),
        )
        drawn, voiced = await asyncio.gather(
            slides.write_deck(plan, parts),
            narrate.synthesise_all(voice, sid, speakers, parts),
            return_exceptions=True,
        )
        # Whatever was voiced is kept on the row before a failure is raised.
        await persist(sid, spend, slides=[p.as_json() for p in parts])
        for outcome in (drawn, voiced):
            if isinstance(outcome, BaseException):
                raise outcome
        assert isinstance(drawn, slides.DeckOutcome)
        log.info(
            "output %s: %d slides on %s (%d drawn plain), %dk characters in, %dk out",
            sid,
            len(drawn.written),
            plan.model,
            drawn.fallbacks,
            drawn.chars_in // 1000,
            drawn.chars_out // 1000,
        )
        fields["deck_ref"] = {"collection": str(sid), "presentation": slides.PRESENTATION}
    await progress.phase_done(PHASES[3])
    await persist(sid, spend, slides=[p.as_json() for p in parts], **fields)

    await progress.phase(PHASES[4])
    validate.narration_is_playable(parts)
    if audio is None:
        validate.slides_are_written(sid, parts)
    await progress.phase_done(PHASES[4])

    # `ready` is written here and nowhere else.
    await persist(sid, spend, state="ready", failure=None)
    log.info("output %s ready", sid)
