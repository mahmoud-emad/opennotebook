"""Mind maps of a collection's sources: the topics they cover, as a tree."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, computed_field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import defer

from opennotebook import jobs
from opennotebook.ai import client, ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.api.sessions import JobOut, SessionEstimate
from opennotebook.db.models import Job, MindMap, User
from opennotebook.db.session import release, sessionmaker
from opennotebook.domain import collections, making, reading
from opennotebook.domain import settings as config
from opennotebook.domain.reading import Estimate
from opennotebook.domain.sources import one_line
from opennotebook.errors import Problem, not_found
from opennotebook.jobs.app import WORK_QUEUE
from opennotebook.script import mindmap as mm
from opennotebook.script.errors import ScriptError, problem, too_slow
from opennotebook.script.mindmap import NamedDoc

router = APIRouter(prefix="/api/collections/{cid}/mindmaps", tags=["mindmaps"])


class MindNode(BaseModel):
    name: str
    children: list[MindNode] = Field(default_factory=list[Any])


class MindMapSummary(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    focus: str
    node_count: int
    sources: list[str]
    shape: list[int] = Field(description="Subtopics per main topic, in order: the outline")
    created_at: datetime
    state: Literal["making", "ready"] = Field(
        default="ready",
        description="`making` while its job draws it: empty until then. One whose making "
        "fails is removed, and its job says why",
    )
    job_id: uuid.UUID | None = Field(
        default=None, description="The job that makes it: follow it at /api/jobs/{id}"
    )

    @field_validator("state", mode="before")
    @classmethod
    def _written(cls, v: object) -> object:
        """A row not written yet has no state: it is a finished one."""
        return "ready" if v is None else v

    @computed_field(
        description="Its name as it is shown: the title, or `Untitled mind map` while it has none"
    )
    @property
    def display_title(self) -> str:
        return " ".join(self.title.split()) or "Untitled mind map"


class MindMapOut(MindMapSummary):
    excerpted: bool = Field(description="The sources were cut to excerpts to fit")
    model: str
    dropped: int = Field(description="Nodes removed because the sources never mention them")
    unchecked: bool = Field(description="The check against the sources did not run")
    root: MindNode


class MakeReq(BaseModel):
    focus: str = Field(default="", max_length=400)
    sources: list[str] | None = Field(default=None, description="Source names; all when absent")


class Retitle(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MakingMap(BaseModel):
    """A map being made: its job, which says how far it got and, when it
    fails, why; and its row, `making` until the job draws it."""

    job: JobOut
    mindmap: MindMapSummary


# What a map being made holds until it is drawn.
NOT_YET: dict[str, Any] = {"name": "", "children": []}


async def _one(s: Db, owner: uuid.UUID, cid: uuid.UUID, mid: uuid.UUID) -> MindMap:
    m = await s.scalar(
        select(MindMap).where(
            MindMap.id == mid, MindMap.collection_id == cid, MindMap.owner_id == owner
        )
    )
    if m is None:
        raise not_found("That mind map")
    return m


@router.get("")
async def list_mindmaps(cid: uuid.UUID, s: Db, me: Me) -> list[MindMapSummary]:
    """A collection's mind maps, newest first."""
    await collections.owned(s, me.id, cid)
    # Without their trees: a list shows each map's outline (`shape`), never
    # its nodes.
    rows = await s.scalars(
        select(MindMap)
        .options(defer(MindMap.root, raiseload=True))
        .where(MindMap.collection_id == cid, MindMap.owner_id == me.id)
        .order_by(MindMap.created_at.desc())
    )
    listed = await making.settle(s, list(rows))
    return [MindMapSummary.model_validate(m, from_attributes=True) for m in listed]


# How long a map may take. One call over at most about 150k tokens, asked
# twice when the first is thin; a minute is far past what that needs and short
# of a person giving up on the page.
CREATE_TIMEOUT_S = 60

# The root's name when nothing better is known; never a map's title when its
# collection has one.
GENERIC_ROOT = "Your sources"


def map_title(root: str, collection: str | None) -> str:
    """A map is titled by its root topic, unless that is the generic "Your
    sources", which would name every map of every collection the same."""
    root = root.strip()
    if not root or root.lower() == GENERIC_ROOT.lower():
        return collection or GENERIC_ROOT
    return root


def shape(root: dict[str, Any]) -> list[int]:
    """Subtopics per main topic, in order: the outline a cover draws."""
    return [len(c.get("children", [])) for c in root.get("children", [])]


def tokens_for(chars: int) -> tuple[int, int]:
    """Tokens in and out of one map call for `chars` of source text.

    In: the text at four characters a token, capped where the generator
    switches to excerpts, plus about 500 for the prompt and the source
    headers. Out: 1,000, above what the measured maps used (20 to 75 nodes of
    a few words each, two spaces of indent a level), so the estimate does not
    undershoot.
    """
    sent = min(chars, mm.WHOLE_TEXT_CHARS)
    return -(-sent // 4) + 500, 1_000


async def _asked(s: Db, me: User, cid: uuid.UUID, body: MakeReq) -> list[NamedDoc]:
    """Everything a map is checked for before it is started: the collection
    is there and may be changed, the sources asked for are there, and the
    spending limit allows it. Lets the transaction go to read the price list."""
    await collections.editable(s, me.id, cid)
    docs = await reading.read_docs(s, me.id, cid, body.sources)
    model = await config.value(s, me.id, config.MINDMAP_MODEL_KEY)
    limit = await reading.limit_of(s, me.id)
    await release(s)
    # Checked against the spending limit before any model call, like a build.
    reading.refuse_over_limit(
        await reading.estimate(docs, model, tokens_for, limit), "A mind map of these sources"
    )
    return docs


async def _start(s: Db, me: User, cid: uuid.UUID, body: MakeReq) -> tuple[Job, MindMap]:
    docs = await _asked(s, me, cid, body)
    return await making.start(
        s,
        me.id,
        cid,
        MindMap,
        "mindmap",
        focus=body.focus.strip(),
        sources=[d.name for d in docs],
        root=NOT_YET,
    )


def _args(job: Job, m: MindMap, body: MakeReq) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "owner_id": str(m.owner_id),
        "collection_id": str(m.collection_id),
        "made_id": str(m.id),
        "focus": body.focus,
        "sources": body.sources,
    }


@router.post("", status_code=202)
async def make_mindmap(cid: uuid.UUID, body: MakeReq, s: Db, me: Me) -> MakingMap:
    """Map the topics a collection's sources cover, in the background: the
    map is listed at once as `making`, and drawn in a few seconds. Follow its
    job at /api/jobs/{id}; when it fails, the map is removed and the job
    says why. Refused at once when the collection cannot be changed, a
    source asked for is not there, or the map could cost more than the
    spending limit."""
    job, m = await _start(s, me, cid, body)
    await jobs.defer(s, job, making.MAP_TASK, _args(job, m, body), queue=WORK_QUEUE)
    return MakingMap(
        job=JobOut.of(job), mindmap=MindMapSummary.model_validate(m, from_attributes=True)
    )


async def make_mindmap_now(cid: uuid.UUID, body: MakeReq, s: Db, me: User) -> MindMapOut:
    """A map made while the caller waits, for the chat agent and the old
    JSON-RPC API, which answer with the map itself: the same checks and the
    same job, run here rather than on the queue."""
    job, m = await _start(s, me, cid, body)
    await release(s)
    if failed := await fill(job.id, me.id, cid, m.id, body.focus, body.sources):
        raise failed
    done = await s.scalar(
        select(MindMap).where(MindMap.id == m.id).execution_options(populate_existing=True)
    )
    if done is None:
        raise not_found("That mind map")
    return MindMapOut.model_validate(done, from_attributes=True)


async def fill(
    job_id: uuid.UUID,
    owner: uuid.UUID,
    cid: uuid.UUID,
    mid: uuid.UUID,
    focus: str,
    sources: list[str] | None,
) -> Problem | None:
    """Draw a `making` map: the job, run by the worker or inline. None when
    it was drawn, else why not."""

    async def make(say: Callable[[str], Awaitable[None]]) -> dict[str, Any]:
        await say("Reading the sources")
        async with sessionmaker()() as s:
            docs = await reading.read_docs(s, owner, cid, sources)
            c = await collections.owned(s, owner, cid)
            model = await config.value(s, owner, config.MINDMAP_MODEL_KEY)
            rule = config.language_rule(await config.value(s, owner, config.LANGUAGE_KEY))
        named = one_line(c.title) or None
        # The root's name when the model leaves it out: the one source's own
        # title, else the collection's, else a plain word for several.
        hint = docs[0].title if len(docs) == 1 else named or GENERIC_ROOT
        await say("Drawing the mind map")
        try:
            async with (
                ledger.spending(owner, "mindmap", collection_id=cid, job_id=job_id),
                asyncio.timeout(CREATE_TIMEOUT_S),
            ):
                made = await mm.generate_map(
                    docs, hint, focus.strip() or None, model=model, language_rule=rule
                )
        except TimeoutError as e:
            raise too_slow(CREATE_TIMEOUT_S, "map the sources") from e
        except (AiError, ScriptError) as e:
            raise problem(e) from e
        root = made.root.to_json()
        return {
            "title": map_title(made.root.name, named),
            "sources": [d.name for d in docs],
            "excerpted": made.excerpted,
            "model": made.model,
            "node_count": made.root.count(),
            "dropped": made.dropped,
            "unchecked": made.unchecked,
            "shape": shape(root),
            "root": root,
        }

    return await making.run(job_id, owner, cid, MindMap, mid, make, "the mind map")


async def one_call(s: Db, owner: uuid.UUID, cid: uuid.UUID) -> Estimate:
    """What the one call that maps every source would cost."""
    await collections.owned(s, owner, cid)
    docs = await reading.read_docs(s, owner, cid, None)
    model = await config.value(s, owner, config.MINDMAP_MODEL_KEY)
    limit = await reading.limit_of(s, owner)
    # The price list can take seconds to read; no transaction waits for it.
    await release(s)
    return await reading.estimate(docs, model, tokens_for, limit)


@router.get("/estimate")
async def estimate_mindmap(cid: uuid.UUID, s: Db, me: Me) -> SessionEstimate:
    """What making a mind map of every source would cost, before making it,
    itemised the way a build's estimate is."""
    q = await one_call(s, me.id, cid)
    price = await client.ai().catalogue.price(q.model)
    return SessionEstimate.one_call(q, price, "Mind map", "Draw the mind map")


@router.get("/{mid}")
async def get_mindmap(cid: uuid.UUID, mid: uuid.UUID, s: Db, me: Me) -> MindMapOut:
    return MindMapOut.model_validate(await _one(s, me.id, cid, mid), from_attributes=True)


@router.patch("/{mid}")
async def retitle_mindmap(
    cid: uuid.UUID, mid: uuid.UUID, body: Retitle, s: Db, me: Me
) -> MindMapSummary:
    m = await _one(s, me.id, cid, mid)
    await collections.editable(s, me.id, cid)
    m.title = " ".join(body.title.split())
    return MindMapSummary.model_validate(m, from_attributes=True)


@router.delete("/{mid}", status_code=204)
async def delete_mindmap(cid: uuid.UUID, mid: uuid.UUID, s: Db, me: Me) -> None:
    """Delete a map; one still being made is stopped first."""
    m = await _one(s, me.id, cid, mid)
    await collections.refuse_read_only(s, await collections.lock(s, me.id, cid))
    if m.state == "making" and m.job_id is not None:
        await jobs.cancel(s, m.job_id)
    await s.execute(delete(MindMap).where(MindMap.id == mid))
    await collections.touch(s, cid)
