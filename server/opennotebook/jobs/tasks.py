"""The work the queue runs: a build (deck or audio overview), a video of
one, and deep research. Each task reads its arguments strictly, and a task whose arguments
do not decode fails its job row with that rather than doing something
else."""

import asyncio
import contextlib
import logging
import uuid

from pydantic import BaseModel, ConfigDict, ValidationError

from opennotebook import research, storage
from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.build import pipeline, video
from opennotebook.build.errors import Abandoned, BuildError
from opennotebook.db.models import Session
from opennotebook.db.session import sessionmaker
from opennotebook.domain import refresh
from opennotebook.domain import settings as st
from opennotebook.errors import SERVER_FAULT, Problem
from opennotebook.jobs import NoJob, Progress
from opennotebook.jobs.app import REFRESH_QUEUE, REFRESH_TASK, app

log = logging.getLogger(__name__)

PREP_TASK = "prep"
RESEARCH_TASK = "research"
RENDER_TASK = video.RENDER_TASK

# Why a job whose arguments do not decode failed, as a person reads it; the
# detail goes to the log.
UNREADABLE = (
    "This work reached the worker in a form it could not read, so it was not started. "
    "Start it again; if it keeps happening, the api and the worker may be running different "
    "versions, so restart both."
)


async def _fail_unreadable(job_id: object, e: ValidationError) -> None:
    try:
        jid = uuid.UUID(str(job_id))
    except ValueError:
        log.error("a job arrived with no readable job id: %s", e)
        return
    log.error("job %s: its arguments did not decode: %s", jid, e)
    await Progress(jid, 0).finish(UNREADABLE)


@app.task(name=PREP_TASK, pass_context=False)
async def prep(**spec: object) -> None:
    """Build a deck or an audio overview."""
    try:
        parsed = pipeline.PrepSpec.model_validate(spec)
    except ValidationError as e:
        await _fail_unreadable(spec.get("job_id"), e)
        return
    await pipeline.run(parsed)


class ResearchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    owner_id: uuid.UUID
    collection_id: uuid.UUID
    topic: str


# Research runs one phase; what it is doing inside it is the step text.
RESEARCH_PHASES = 1


@app.task(name=RESEARCH_TASK, pass_context=False)
async def deep_research(**spec: object) -> None:
    """Research a topic and add the report to the collection as a source."""
    try:
        r = ResearchSpec.model_validate(spec)
    except ValidationError as e:
        await _fail_unreadable(spec.get("job_id"), e)
        return
    progress = Progress(r.job_id, RESEARCH_PHASES)

    async def say(step: str) -> None:
        await progress.phase(step)

    async with ledger.spending(
        r.owner_id, "research", collection_id=r.collection_id, job_id=r.job_id
    ):
        try:
            await progress.start()
            async with sessionmaker()() as s:
                values = await st.values(s, r.owner_id)
            found = await research.gather(values, r.topic, say)
            await pipeline.add_research(r.owner_id, r.collection_id, found)
        except NoJob:
            # The collection was deleted, and the job row with it.
            return
        except asyncio.CancelledError:
            await asyncio.shield(progress.cancelled())
            raise
        except (research.ResearchError, AiError) as e:
            await _finish(progress, e.sentence)
            return
        except Problem as e:
            # The collection is gone: nothing to add the report to.
            await _finish(progress, e.detail)
            return
        except Exception:
            log.exception("research for %s failed", r.collection_id)
            await _finish(progress, SERVER_FAULT)
            return
    progress.done = RESEARCH_PHASES
    await _finish(progress, None)
    # The collection's sources changed: name it and design its cover again,
    # merged with any other change to it.
    await refresh.request(r.owner_id, r.collection_id)


class RefreshSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: uuid.UUID
    collection_id: uuid.UUID


@app.task(name=REFRESH_TASK, queue=REFRESH_QUEUE, pass_context=False)
async def refresh_collection(**spec: object) -> None:
    """Name a collection and design its cover, after its sources changed.
    Queued by `refresh.schedule`, one waiting per collection at most."""
    try:
        r = RefreshSpec.model_validate(spec)
    except ValidationError as e:
        log.error("a refresh arrived with arguments that do not decode: %s", e)
        return
    await refresh.refresh(r.owner_id, r.collection_id)


async def _finish(progress: Progress, error: str | None) -> None:
    with contextlib.suppress(NoJob):
        await progress.finish(error)


# Why a render that was stopped did not finish, as the output's video says.
STOPPED = "The video was stopped before it was finished. Make it again to start over."


class RenderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    session_id: uuid.UUID
    style: str
    # A whiteboard's theme (build/whiteboard/theme.py); a render queued
    # before themes has none and is drawn as the whiteboard.
    theme: str = "whiteboard"


@app.task(name=RENDER_TASK, pass_context=False)
async def render_video(**spec: object) -> None:
    """Make a video of a finished output. Its result goes on the output's
    `video` field; the output's own state is never touched, so a failed
    render leaves a ready output ready."""
    try:
        r = RenderSpec.model_validate(spec)
    except ValidationError as e:
        await _fail_unreadable(spec.get("job_id"), e)
        return
    progress = Progress(r.job_id, len(video.phases_of(r.style)))
    try:
        await progress.start()
        async with sessionmaker()() as s:
            o = await s.get(Session, r.session_id)
            if o is None:
                raise Abandoned(r.session_id)
            owner, cid = o.owner_id, o.collection_id
            values = await st.values(s, owner)
            models = video.Models(
                write=str(values[st.VIDEO_MODEL_KEY]),
                check=str(values[st.VIDEO_CHECK_MODEL_KEY]),
                escalate=str(values[st.VIDEO_ESCALATE_MODEL_KEY]),
                image=str(values[st.VIDEO_IMAGE_MODEL_KEY]),
            )
        async with ledger.spending(
            owner, "video", collection_id=cid, session_id=r.session_id, job_id=r.job_id
        ) as spend:
            done = await video.render(
                r.session_id, r.style, progress.phase, progress.phase_done, models, theme=r.theme
            )
        done["spent_usd"] = float(spend.total_usd)
        done["spent_known"] = spend.known
        await video.set_state(r.session_id, r.style, **done)
    except NoJob, Abandoned:
        # The output was deleted, and its job row with it. Its delete removed
        # its files when it happened; a render that wrote after that removes
        # its own, or they would be kept forever.
        if await video.gone(r.session_id):
            await asyncio.to_thread(storage.remove_tree, video.video_dir(r.session_id))
        return
    except asyncio.CancelledError:
        with contextlib.suppress(Abandoned):
            await asyncio.shield(
                video.set_state(r.session_id, r.style, state="failed", failure=STOPPED)
            )
        await asyncio.shield(progress.cancelled())
        raise
    except Exception as e:
        if isinstance(e, BuildError | AiError):
            log.info("video of %s failed: %s", r.session_id, e)
            sentence = e.sentence
        else:
            log.exception("video of %s failed", r.session_id)
            sentence = SERVER_FAULT
        try:
            await video.set_state(r.session_id, r.style, state="failed", failure=sentence)
        except Abandoned:
            return
        # A build failure names what failed; anything else is said by its
        # kind only, since its message can carry the server's own paths.
        await _finish(progress, str(e) if isinstance(e, BuildError) else type(e).__name__)
        return
    await _finish(progress, None)
