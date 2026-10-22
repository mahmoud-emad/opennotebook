"""Transformations: named instructions run over a collection's sources, and
a person's own to add to the built-in ones."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.api.notes import Citation
from opennotebook.api.sources import SourceOut
from opennotebook.db.models import Transformation
from opennotebook.db.session import release
from opennotebook.domain import collections, reading, refresh, sources
from opennotebook.domain import settings as config
from opennotebook.domain import transformations as tf
from opennotebook.errors import Problem
from opennotebook.script import transform
from opennotebook.script.errors import ScriptError, problem

router = APIRouter(prefix="/api", tags=["transformations"])


class TransformationOut(BaseModel):
    id: str = Field(description="What to run it by: a key for a built-in one, else an id")
    name: str
    description: str = Field(description="One line on what it makes; empty for a person's own")
    prompt: str = Field(description="The instruction it carries out")
    builtin: bool = Field(description="Built in: it cannot be changed or removed")

    @classmethod
    def of(cls, f: tf.Found) -> TransformationOut:
        return cls(
            id=f.id, name=f.name, description=f.description, prompt=f.prompt, builtin=f.builtin
        )


class TransformationIn(BaseModel):
    name: str = Field(description="A short name, unique among yours")
    prompt: str = Field(description="What to do with the sources")


@router.get("/transformations")
async def list_transformations(s: Db, me: Me) -> list[TransformationOut]:
    """The built-in transformations, then your own."""
    return [TransformationOut.of(f) for f in await tf.listed(s, me.id)]


def _taken(name: str) -> Problem:
    return Problem(
        409, f"You already have a transformation called “{name}”. Pick another name, or change it."
    )


@router.post("/transformations", status_code=201)
async def add_transformation(body: TransformationIn, s: Db, me: Me) -> TransformationOut:
    """Add a transformation of your own."""
    name, prompt = tf.clean(body.name, body.prompt)
    if await tf.count(s, me.id) >= tf.CUSTOM_MAX:
        raise Problem(
            409,
            f"You have {tf.CUSTOM_MAX} transformations, the most there can be. "
            "Remove one you no longer use, then add this one.",
        )
    row = Transformation(owner_id=me.id, name=name, prompt=prompt)
    s.add(row)
    try:
        await s.flush()
    except IntegrityError:
        raise _taken(name) from None
    await s.refresh(row)
    return TransformationOut.of(tf.of(row))


@router.put("/transformations/{tid}")
async def change_transformation(
    tid: str, body: TransformationIn, s: Db, me: Me
) -> TransformationOut:
    """Change a transformation of your own."""
    _refuse_builtin(tid)
    row = await tf.one(s, me.id, tid)
    row.name, row.prompt = tf.clean(body.name, body.prompt)
    try:
        await s.flush()
    except IntegrityError:
        raise _taken(row.name) from None
    return TransformationOut.of(tf.of(row))


@router.delete("/transformations/{tid}", status_code=204)
async def remove_transformation(tid: str, s: Db, me: Me) -> None:
    """Remove a transformation of your own."""
    _refuse_builtin(tid)
    await s.delete(await tf.one(s, me.id, tid))


def _refuse_builtin(key: str) -> None:
    if (b := tf.builtin(key)) is not None:
        raise Problem(
            422,
            f"“{b.name}” is built in, so it cannot be changed or removed. "
            "Add a transformation of your own to do it differently.",
        )


class TransformReq(BaseModel):
    transformation: str = Field(description="A built-in key, an id, or a name")
    sources: list[str] | None = Field(default=None, description="Source names; all that are on")
    focus: str = Field(default="", max_length=1_000, description="What to concentrate on")
    save: bool = Field(default=True, description="Keep the result as a note in the sources")


class TransformOut(BaseModel):
    title: str = Field(description="The transformation's name, and its focus when given")
    text: str = Field(description="Markdown with [n] markers that match the citations")
    citations: list[Citation] = Field(description="Only the passages cited, in order of first use")
    source: SourceOut | None = Field(description="The note it was kept as; absent when not saved")


def note_text(text: str, cited: list[Citation]) -> str:
    """The result as the text of a note: itself, then the passages it cites."""
    if not cited:
        return text
    lines = [f"{c.n}. {c.title}" + (f" ({c.url})" if c.url else "") for c in cited]
    return f"{text}\n\nSources:\n\n" + "\n".join(lines)


@router.post("/collections/{cid}/transform")
async def run_transformation(cid: uuid.UUID, body: TransformReq, s: Db, me: Me) -> TransformOut:
    """Run a transformation over the collection's sources (or only the named
    ones) and, unless told not to, keep the result as a note. Takes a few
    seconds."""
    if body.save:
        await collections.editable(s, me.id, cid)
    else:
        await collections.owned(s, me.id, cid)
    found = await tf.find(s, me.id, body.transformation)
    focus = body.focus.strip()
    docs = await reading.read_docs(s, me.id, cid, body.sources)
    model = await config.value(s, me.id, config.CHAT_MODEL_KEY)
    rule = config.language_rule(await config.value(s, me.id, config.LANGUAGE_KEY))
    # Nothing is written: the transaction goes before the model is asked.
    await release(s)
    try:
        async with ledger.spending(me.id, "transform", collection_id=cid):
            got = await transform.run(docs, found.prompt, focus, model=model, language_rule=rule)
    except (AiError, ScriptError) as e:
        raise problem(e) from e
    cited = [
        Citation(
            n=c.n,
            name=docs[c.doc].name,
            title=docs[c.doc].title,
            url=docs[c.doc].url,
            excerpt=c.excerpt,
        )
        for c in got.cited
    ]
    title = f"{found.name}: {focus}" if focus else found.name
    kept: SourceOut | None = None
    if body.save:
        try:
            ready = await sources.prepare_note(note_text(got.text, cited), title)
        except sources.Refused as e:
            raise Problem(422, str(e)) from e
        kept = SourceOut.of(await sources.keep(s, me.id, cid, ready))
        await refresh.schedule(s, me.id, cid)
    return TransformOut(title=title, text=got.text, citations=cited, source=kept)
