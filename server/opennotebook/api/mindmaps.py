"""Mind maps of a collection's sources: the topics they cover, as a tree."""

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.db.models import MindMap
from opennotebook.domain import collections, reading
from opennotebook.domain import settings as config
from opennotebook.domain.reading import Estimate
from opennotebook.domain.sources import one_line
from opennotebook.errors import Problem, not_found
from opennotebook.script import mindmap as mm
from opennotebook.script.errors import ScriptError, problem, too_slow

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
    await collections.summary(s, me.id, cid)
    rows = await s.scalars(
        select(MindMap)
        .where(MindMap.collection_id == cid, MindMap.owner_id == me.id)
        .order_by(MindMap.created_at.desc())
    )
    return [MindMapSummary.model_validate(m, from_attributes=True) for m in rows]


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


@router.post("", status_code=201)
async def make_mindmap(cid: uuid.UUID, body: MakeReq, s: Db, me: Me) -> MindMapOut:
    """Map the topics a collection's sources cover. Takes a few seconds."""
    summary = await collections.summary(s, me.id, cid)
    await collections.refuse_read_only(s, summary.collection)
    docs = await reading.read_docs(s, me.id, cid, body.sources)
    named = one_line(summary.collection.title) or None
    # The root's name when the model leaves it out: the one source's own
    # title, else the collection's, else a plain word for several.
    hint = docs[0].title if len(docs) == 1 else named or GENERIC_ROOT
    focus = body.focus.strip() or None
    model = await config.value(s, me.id, config.MINDMAP_MODEL_KEY)
    rule = config.language_rule(await config.value(s, me.id, config.LANGUAGE_KEY))
    try:
        async with (
            ledger.spending(me.id, "mindmap", collection_id=cid),
            asyncio.timeout(CREATE_TIMEOUT_S),
        ):
            made = await mm.generate_map(docs, hint, focus, model=model, language_rule=rule)
    except TimeoutError as e:
        raise too_slow(CREATE_TIMEOUT_S, "map the sources") from e
    except (AiError, ScriptError) as e:
        raise problem(e) from e
    # Kept only while the collection is: a minute of model time is long
    # enough for somebody to delete it, and a map saved after that would
    # bring it back.
    try:
        await collections.lock(s, me.id, cid)
    except Problem as e:
        raise Problem(
            404, "The collection was deleted while the map was being made, so nothing was kept."
        ) from e
    root = made.root.to_json()
    m = MindMap(
        owner_id=me.id,
        collection_id=cid,
        title=map_title(made.root.name, named),
        focus=focus or "",
        sources=[d.name for d in docs],
        excerpted=made.excerpted,
        model=made.model,
        node_count=made.root.count(),
        dropped=made.dropped,
        unchecked=made.unchecked,
        shape=shape(root),
        root=root,
    )
    s.add(m)
    await s.flush()
    await s.refresh(m)
    await collections.touch(s, cid)
    return MindMapOut.model_validate(m, from_attributes=True)


@router.get("/estimate")
async def estimate_mindmap(cid: uuid.UUID, s: Db, me: Me) -> Estimate:
    """What making a mind map of every source would cost, before making it."""
    await collections.summary(s, me.id, cid)
    docs = await reading.read_docs(s, me.id, cid, None)
    model = await config.value(s, me.id, config.MINDMAP_MODEL_KEY)
    return await reading.estimate(docs, model, tokens_for)


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
    await _one(s, me.id, cid, mid)
    await collections.refuse_read_only(s, await collections.lock(s, me.id, cid))
    await s.execute(delete(MindMap).where(MindMap.id == mid))
    await collections.touch(s, cid)
