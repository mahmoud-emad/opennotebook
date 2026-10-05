"""The work the queue runs: a build (deck or audio overview) and deep
research. Each task reads its arguments strictly, and a task whose arguments
do not decode fails its job row with that rather than doing something
else."""

import asyncio
import contextlib
import logging
import uuid

from pydantic import BaseModel, ConfigDict, ValidationError

from opennotebook import research
from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.build import pipeline
from opennotebook.db.session import sessionmaker
from opennotebook.domain import refresh
from opennotebook.domain import settings as st
from opennotebook.errors import SERVER_FAULT, Problem
from opennotebook.jobs import NoJob, Progress
from opennotebook.jobs.app import app

log = logging.getLogger(__name__)

PREP_TASK = "prep"
RESEARCH_TASK = "research"


async def _fail_unreadable(job_id: object, e: ValidationError) -> None:
    try:
        jid = uuid.UUID(str(job_id))
    except ValueError:
        log.error("a job arrived with no readable job id: %s", e)
        return
    await Progress(jid, 0).finish(f"the job's arguments did not decode: {e}")


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
    # The collection's sources changed: name it and design its cover again.
    await refresh.refresh(r.owner_id, r.collection_id)


async def _finish(progress: Progress, error: str | None) -> None:
    with contextlib.suppress(NoJob):
        await progress.finish(error)
