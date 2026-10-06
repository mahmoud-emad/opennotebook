"""Study notes of a collection's sources: key ideas, a quiz, essay questions
and a glossary, every claim cited to its passage."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import (
    ARRAY,
    ColumnElement,
    Select,
    Text,
    delete,
    func,
    literal_column,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from opennotebook import jobs
from opennotebook.ai import client, ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.api.mindmaps import MakeReq, Retitle
from opennotebook.api.sessions import JobOut, SessionEstimate
from opennotebook.db.models import Job, StudyNotes, User
from opennotebook.db.session import release, sessionmaker
from opennotebook.domain import collections, making, reading
from opennotebook.domain import settings as config
from opennotebook.domain.reading import Estimate
from opennotebook.errors import Problem, not_found
from opennotebook.jobs.app import WORK_QUEUE
from opennotebook.script import notes as sn
from opennotebook.script.errors import ScriptError, problem, too_slow
from opennotebook.script.mindmap import NamedDoc

router = APIRouter(prefix="/api/collections/{cid}/notes", tags=["notes"])


class NoteIdea(BaseModel):
    heading: str
    body: str = Field(description="Markdown with [n] markers")


class NoteTerm(BaseModel):
    term: str
    definition: str


class NoteQuestion(BaseModel):
    question: str
    answer: str


class Citation(BaseModel):
    n: int = Field(description="The number in the text, [1], [2], in reading order")
    name: str = Field(description="The source's name")
    title: str
    url: str
    excerpt: str = Field(description="The passage the claim rests on")


class NotesSummary(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    display_title: str = Field(
        description="Its name as it is shown: the title, or `Untitled study notes` while it "
        "has none"
    )
    focus: str
    sources: list[str]
    created_at: datetime
    ideas: int
    questions: int
    terms: int
    headings: list[str]
    state: Literal["making", "ready"] = Field(
        default="ready",
        description="`making` while its job writes them: empty until then. Notes whose "
        "writing fails are removed, and their job says why",
    )
    job_id: uuid.UUID | None = Field(
        default=None, description="The job that writes them: follow it at /api/jobs/{id}"
    )

    @classmethod
    def of(cls, n: StudyNotes) -> NotesSummary:
        b = n.body
        return cls.counted(
            n,
            len(b.get("ideas", [])),
            len(b.get("quiz", [])),
            len(b.get("glossary", [])),
            [i.get("heading", "") for i in b.get("ideas", [])],
        )

    @classmethod
    def counted(
        cls, n: StudyNotes, ideas: int, questions: int, terms: int, headings: list[str]
    ) -> NotesSummary:
        """Notes as a list shows them, from counts read in the database
        (`listed`) rather than from their body."""
        return cls(
            id=n.id,
            collection_id=n.collection_id,
            title=n.title,
            display_title=" ".join(n.title.split()) or "Untitled study notes",
            focus=n.focus,
            sources=n.sources,
            created_at=n.created_at,
            ideas=ideas,
            questions=questions,
            terms=terms,
            headings=headings,
            # A row not written yet has no state: it is a finished one.
            state=n.state or "ready",  # pyright: ignore[reportArgumentType]
            job_id=n.job_id,
        )


def _length(key: str) -> ColumnElement[int]:
    """How many entries one list of a body holds, 0 when it has none."""
    return func.coalesce(
        func.jsonb_array_length(func.coalesce(StudyNotes.body[key], text("'[]'::jsonb"))), 0
    )


def listed() -> Select[StudyNotes, int, int, int, Sequence[str]]:
    """Notes as a list shows them: the counts and the idea headings, read in
    the database; the body itself, every idea, answer and citation, stays
    there."""
    headings = literal_column(
        "ARRAY(SELECT coalesce(idea ->> 'heading', '') FROM jsonb_array_elements("
        "coalesce(study_notes.body -> 'ideas', '[]'::jsonb)) WITH ORDINALITY AS i(idea, n) "
        "ORDER BY n)",
        ARRAY(Text),
    )
    return select(
        StudyNotes, _length("ideas"), _length("quiz"), _length("glossary"), headings
    ).options(defer(StudyNotes.body, raiseload=True))


class NotesOut(NotesSummary):
    excerpted: bool
    model: str
    dropped: int = Field(description="Citations removed: naming no passage, or not saying it")
    unchecked: bool
    overview: str
    idea_list: list[NoteIdea]
    quiz: list[NoteQuestion]
    essays: list[str]
    glossary: list[NoteTerm]
    citations: list[Citation]
    markdown: str = Field(description="The whole notes as one Markdown document: the export")

    @classmethod
    def full(cls, n: StudyNotes) -> NotesOut:
        b: dict[str, Any] = n.body
        return cls(
            **NotesSummary.of(n).model_dump(),
            excerpted=n.excerpted,
            model=n.model,
            dropped=n.dropped,
            unchecked=n.unchecked,
            overview=b.get("overview", ""),
            idea_list=b.get("ideas", []),
            quiz=b.get("quiz", []),
            essays=b.get("essays", []),
            glossary=b.get("glossary", []),
            citations=b.get("citations", []),
            markdown=b.get("markdown", ""),
        )


async def _one(s: Db, owner: uuid.UUID, cid: uuid.UUID, nid: uuid.UUID) -> StudyNotes:
    n = await s.scalar(
        select(StudyNotes).where(
            StudyNotes.id == nid, StudyNotes.collection_id == cid, StudyNotes.owner_id == owner
        )
    )
    if n is None:
        raise not_found("Those notes")
    return n


@router.get("")
async def list_notes(cid: uuid.UUID, s: Db, me: Me) -> list[NotesSummary]:
    """A collection's study notes, newest first."""
    await collections.owned(s, me.id, cid)
    return await notes_of(s, me.id, cid)


async def notes_of(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> list[NotesSummary]:
    """A collection's study notes, newest first, as its list and its event
    stream give them."""
    rows = list(
        await s.execute(
            listed()
            .where(StudyNotes.collection_id == cid, StudyNotes.owner_id == owner)
            .order_by(StudyNotes.created_at.desc())
        )
    )
    kept = set(await making.settle(s, [n for n, *_ in rows]))
    return [NotesSummary.counted(n, i, q, t, list(h)) for n, i, q, t, h in rows if n in kept]


# How long a set of notes may take. One call that reads up to about 150k
# tokens and writes about 4k, asked twice when the first is thin; the writing
# is what takes the time.
CREATE_TIMEOUT_S = 120

# Tokens a typical set of notes is written in: about 2,500 words across the
# overview, five ideas, ten questions and answers, five essays and twenty
# terms, with their markers.
OUTPUT_TOKENS = 4_000


def tokens_in(chars: int) -> int:
    """Tokens into one call for `chars` of source text: the text at four
    characters a token, capped where the writer switches to excerpts, plus
    about 1,200 for the prompt and one passage label per 900 characters."""
    sent = min(chars, sn.WHOLE_TEXT_CHARS)
    return -(-sent // 4) + 1_200 + -(-sent // 900) * 6


def body_of(made: sn.StudyNotes, docs: list[NamedDoc]) -> dict[str, Any]:
    """Generated notes as the row's body: every section, each citation
    resolved to its source, and the Markdown export."""
    return {
        "overview": made.overview,
        "ideas": [{"heading": i.heading, "body": i.body} for i in made.ideas],
        "quiz": [{"question": q.question, "answer": q.answer} for q in made.quiz],
        "essays": made.essays,
        "glossary": [{"term": t.term, "definition": t.definition} for t in made.glossary],
        "citations": [
            Citation(
                n=c.n,
                name=docs[c.doc].name,
                title=docs[c.doc].title,
                url=docs[c.doc].url,
                excerpt=c.excerpt,
            ).model_dump()
            for c in made.cited
        ],
        "markdown": made.to_markdown(lambda i: docs[i].title if i < len(docs) else ""),
    }


class MakingNotes(BaseModel):
    """Notes being written: their job, which says how far it got and, when
    it fails, why; and their row, `making` until the job writes them."""

    job: JobOut
    notes: NotesSummary


async def _start(s: Db, me: User, cid: uuid.UUID, body: MakeReq) -> tuple[Job, StudyNotes]:
    """Check everything notes are checked for before they are started (the
    collection is there and may be changed, the sources asked for are there,
    the spending limit allows them), then write their `making` row and job."""
    await collections.editable(s, me.id, cid)
    docs = await reading.read_docs(s, me.id, cid, body.sources)
    model = await config.value(s, me.id, config.NOTES_MODEL_KEY)
    limit = await reading.limit_of(s, me.id)
    # The price list is read with no transaction open.
    await release(s)
    # Checked against the spending limit before any model call, like a build.
    reading.refuse_over_limit(
        await reading.estimate(docs, model, lambda chars: (tokens_in(chars), OUTPUT_TOKENS), limit),
        "Study notes of these sources",
    )
    return await making.start(
        s,
        me.id,
        cid,
        StudyNotes,
        "notes",
        focus=body.focus.strip(),
        sources=[d.name for d in docs],
        body={},
    )


@router.post("", status_code=202)
async def make_notes(cid: uuid.UUID, body: MakeReq, s: Db, me: Me) -> MakingNotes:
    """Write study notes of a collection's sources, in the background: they
    are listed at once as `making`, and written in 10 to 40 seconds. Follow
    their job at /api/jobs/{id}; when it fails, the notes are removed and
    the job says why. Refused at once when the collection cannot be changed,
    a source asked for is not there, or the notes could cost more than the
    spending limit."""
    job, n = await _start(s, me, cid, body)
    args = {
        "job_id": str(job.id),
        "owner_id": str(me.id),
        "collection_id": str(cid),
        "made_id": str(n.id),
        "focus": body.focus,
        "sources": body.sources,
    }
    await jobs.defer(s, job, making.NOTES_TASK, args, queue=WORK_QUEUE)
    return MakingNotes(job=JobOut.of(job), notes=NotesSummary.of(n))


async def make_notes_now(cid: uuid.UUID, body: MakeReq, s: Db, me: User) -> NotesOut:
    """Notes written while the caller waits, for the chat agent and the old
    JSON-RPC API, which answer with the notes themselves: the same checks
    and the same job, run here rather than on the queue."""
    job, n = await _start(s, me, cid, body)
    await release(s)
    if failed := await fill(job.id, me.id, cid, n.id, body.focus, body.sources):
        raise failed
    done = await s.scalar(
        select(StudyNotes).where(StudyNotes.id == n.id).execution_options(populate_existing=True)
    )
    if done is None:
        raise not_found("Those notes")
    return NotesOut.full(done)


async def fill(
    job_id: uuid.UUID,
    owner: uuid.UUID,
    cid: uuid.UUID,
    nid: uuid.UUID,
    focus: str,
    sources: list[str] | None,
) -> Problem | None:
    """Write `making` notes: the job, run by the worker or inline. None when
    they were written, else why not."""

    async def make(say: Callable[[str], Awaitable[None]]) -> dict[str, Any]:
        await say("Reading the sources")
        async with sessionmaker()() as s:
            docs = await reading.read_docs(s, owner, cid, sources)
            model = await config.value(s, owner, config.NOTES_MODEL_KEY)
            rule = config.language_rule(await config.value(s, owner, config.LANGUAGE_KEY))
        hint = docs[0].title if len(docs) == 1 else "Study notes"
        await say("Writing the study notes")
        try:
            async with (
                ledger.spending(owner, "notes", collection_id=cid, job_id=job_id),
                asyncio.timeout(CREATE_TIMEOUT_S),
            ):
                made = await sn.generate_notes(
                    docs, hint, focus.strip() or None, model=model, language_rule=rule
                )
        except TimeoutError as e:
            raise too_slow(CREATE_TIMEOUT_S, "write the notes") from e
        except (AiError, ScriptError) as e:
            raise problem(e) from e
        return {
            "title": made.title,
            "sources": [d.name for d in docs],
            "excerpted": made.excerpted,
            "model": made.model,
            "dropped": made.dropped,
            "unchecked": made.unchecked,
            "body": body_of(made, docs),
        }

    return await making.run(job_id, owner, cid, StudyNotes, nid, make, "the study notes")


async def one_call(s: Db, owner: uuid.UUID, cid: uuid.UUID) -> Estimate:
    """What the one call that writes notes of every source would cost."""
    await collections.owned(s, owner, cid)
    docs = await reading.read_docs(s, owner, cid, None)
    model = await config.value(s, owner, config.NOTES_MODEL_KEY)
    limit = await reading.limit_of(s, owner)
    # The price list can take seconds to read; no transaction waits for it.
    await release(s)
    return await reading.estimate(
        docs, model, lambda chars: (tokens_in(chars), OUTPUT_TOKENS), limit
    )


@router.get("/estimate")
async def estimate_notes(cid: uuid.UUID, s: Db, me: Me) -> SessionEstimate:
    """What writing study notes of every source would cost, before writing
    them, itemised the way a build's estimate is."""
    q = await one_call(s, me.id, cid)
    price = await client.ai().catalogue.price(q.model)
    return SessionEstimate.one_call(q, price, "Study notes", "Write the study notes")


@router.get("/{nid}")
async def get_notes(cid: uuid.UUID, nid: uuid.UUID, s: Db, me: Me) -> NotesOut:
    return NotesOut.full(await _one(s, me.id, cid, nid))


@router.patch("/{nid}")
async def retitle_notes(
    cid: uuid.UUID, nid: uuid.UUID, body: Retitle, s: Db, me: Me
) -> NotesSummary:
    n = await _one(s, me.id, cid, nid)
    await collections.editable(s, me.id, cid)
    n.title = " ".join(body.title.split())
    return NotesSummary.of(n)


@router.delete("/{nid}", status_code=204)
async def delete_notes(cid: uuid.UUID, nid: uuid.UUID, s: Db, me: Me) -> None:
    """Delete notes; ones still being written are stopped first."""
    n = await _one(s, me.id, cid, nid)
    await collections.refuse_read_only(s, await collections.lock(s, me.id, cid))
    if n.state == "making" and n.job_id is not None:
        await jobs.cancel(s, n.job_id)
    await s.execute(delete(StudyNotes).where(StudyNotes.id == nid))
    await collections.touch(s, cid)
