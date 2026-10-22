"""Search a collection's sources: the passages most about a query."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from opennotebook import memory
from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.api.deps import Db, Me
from opennotebook.db.models import Source
from opennotebook.domain import collections
from opennotebook.errors import Problem

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collections/{cid}/search", tags=["search"])

DEFAULT_HITS = 8
MAX_HITS = 25


class SearchHit(BaseModel):
    name: str = Field(description="The source it is from; address the source by it")
    title: str
    excerpt: str = Field(description="The passage, verbatim from the source's text")
    start: int = Field(description="Where the passage starts in the source's text")
    end: int = Field(description="Where the passage ends in the source's text")


@router.get("")
async def search_sources(
    cid: uuid.UUID,
    s: Db,
    me: Me,
    q: Annotated[str, Query(max_length=500, description="What to look for")],
    limit: Annotated[int, Query(ge=1, le=MAX_HITS)] = DEFAULT_HITS,
) -> list[SearchHit]:
    """The passages of the collection's sources most about `q`, best first.
    Sources switched off are left out. It matches the words, and the meaning
    too when the studio has an embedding model."""
    query = q.strip()
    if not query:
        raise Problem(422, "The search is empty. Type a word or a question, then search again.")
    await collections.owned(s, me.id, cid)
    try:
        async with ledger.spending(me.id, "search", collection_id=cid):
            found = await memory.search(s, me.id, cid, query, limit)
    except AiError as e:
        # The words still match without the meaning: a search that cannot
        # embed its query is a worse search, not a failed one.
        log.warning("search fell back to the words alone: %s", e)
        found = await memory.search(s, me.id, cid, query, limit, model="")
    titles = dict(
        (
            await s.execute(select(Source.name, Source.title).where(Source.collection_id == cid))
        ).all()
    )
    return [
        SearchHit(
            name=h.source,
            title=titles.get(h.source, "") or h.source,
            excerpt=h.text,
            start=h.start,
            end=h.end,
        )
        for h in found
    ]
