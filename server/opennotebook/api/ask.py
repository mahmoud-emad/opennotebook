"""Ask a collection's sources a question: one answer, every claim cited to
the passage it rests on."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.api.notes import Citation
from opennotebook.db.session import release
from opennotebook.domain import collections, reading
from opennotebook.domain import settings as config
from opennotebook.errors import Problem
from opennotebook.script import cite
from opennotebook.script.errors import ScriptError, problem

router = APIRouter(prefix="/api/collections/{cid}/ask", tags=["ask"])


class AskReq(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    sources: list[str] | None = Field(default=None, description="Source names; all when absent")


class AskOut(BaseModel):
    answer: str = Field(description="Markdown with [n] markers that match the citations")
    citations: list[Citation] = Field(description="Only the passages cited, in order of first use")


@router.post("")
async def ask_sources(cid: uuid.UUID, body: AskReq, s: Db, me: Me) -> AskOut:
    """Answer a question from the collection's sources (or only the named
    ones), with citations. Takes a few seconds."""
    question = body.question.strip()
    if not question:
        raise Problem(422, "The question is empty. Write a question, then ask again.")
    await collections.summary(s, me.id, cid)
    docs = await reading.read_docs(s, me.id, cid, body.sources)
    model = await config.value(s, me.id, config.CHAT_MODEL_KEY)
    rule = config.language_rule(await config.value(s, me.id, config.LANGUAGE_KEY))
    # Nothing is written: the transaction goes before the model is asked.
    await release(s)
    try:
        async with ledger.spending(me.id, "ask", collection_id=cid):
            got = await cite.answer(docs, question, model=model, language_rule=rule)
    except (AiError, ScriptError) as e:
        raise problem(e) from e
    return AskOut(
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
    )
