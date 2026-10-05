"""Study notes of a collection's sources: key ideas, a quiz, essay questions
and a glossary, every claim cited to its passage."""

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
from opennotebook.api.mindmaps import MakeReq, Retitle
from opennotebook.db.models import StudyNotes
from opennotebook.domain import collections, reading
from opennotebook.domain import settings as config
from opennotebook.domain.reading import Estimate
from opennotebook.errors import Problem, not_found
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
    focus: str
    sources: list[str]
    created_at: datetime
    ideas: int
    questions: int
    terms: int
    headings: list[str]

    @classmethod
    def of(cls, n: StudyNotes) -> NotesSummary:
        b = n.body
        return cls(
            id=n.id,
            collection_id=n.collection_id,
            title=n.title,
            focus=n.focus,
            sources=n.sources,
            created_at=n.created_at,
            ideas=len(b.get("ideas", [])),
            questions=len(b.get("quiz", [])),
            terms=len(b.get("glossary", [])),
            headings=[i.get("heading", "") for i in b.get("ideas", [])],
        )


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
    await collections.summary(s, me.id, cid)
    rows = await s.scalars(
        select(StudyNotes)
        .where(StudyNotes.collection_id == cid, StudyNotes.owner_id == me.id)
        .order_by(StudyNotes.created_at.desc())
    )
    return [NotesSummary.of(n) for n in rows]


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


@router.post("", status_code=201)
async def make_notes(cid: uuid.UUID, body: MakeReq, s: Db, me: Me) -> NotesOut:
    """Write study notes of a collection's sources. Takes 10 to 40 seconds."""
    await collections.editable(s, me.id, cid)
    docs = await reading.read_docs(s, me.id, cid, body.sources)
    hint = docs[0].title if len(docs) == 1 else "Study notes"
    focus = body.focus.strip() or None
    model = await config.value(s, me.id, config.NOTES_MODEL_KEY)
    rule = config.language_rule(await config.value(s, me.id, config.LANGUAGE_KEY))
    try:
        async with (
            ledger.spending(me.id, "notes", collection_id=cid),
            asyncio.timeout(CREATE_TIMEOUT_S),
        ):
            made = await sn.generate_notes(docs, hint, focus, model=model, language_rule=rule)
    except TimeoutError as e:
        raise too_slow(CREATE_TIMEOUT_S, "write the notes") from e
    except (AiError, ScriptError) as e:
        raise problem(e) from e
    # Kept only while the collection is; see `make_mindmap`.
    try:
        await collections.lock(s, me.id, cid)
    except Problem as e:
        raise Problem(
            404,
            "The collection was deleted while the notes were being written, so nothing was kept.",
        ) from e
    n = StudyNotes(
        owner_id=me.id,
        collection_id=cid,
        title=made.title,
        focus=focus or "",
        sources=[d.name for d in docs],
        excerpted=made.excerpted,
        model=made.model,
        dropped=made.dropped,
        unchecked=made.unchecked,
        body=body_of(made, docs),
    )
    s.add(n)
    await s.flush()
    await s.refresh(n)
    await collections.touch(s, cid)
    return NotesOut.full(n)


@router.get("/estimate")
async def estimate_notes(cid: uuid.UUID, s: Db, me: Me) -> Estimate:
    """What writing study notes of every source would cost, before writing them."""
    await collections.summary(s, me.id, cid)
    docs = await reading.read_docs(s, me.id, cid, None)
    model = await config.value(s, me.id, config.NOTES_MODEL_KEY)
    return await reading.estimate(docs, model, lambda chars: (tokens_in(chars), OUTPUT_TOKENS))


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
    await _one(s, me.id, cid, nid)
    await collections.refuse_read_only(s, await collections.lock(s, me.id, cid))
    await s.execute(delete(StudyNotes).where(StudyNotes.id == nid))
    await collections.touch(s, cid)
