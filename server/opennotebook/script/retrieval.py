"""Retrieval for the script: both kinds of material for one query. The
`retrieve` half of `opennotebook_script/src/grounding.rs`; the excerpting it
uses is `script.grounding`.

The script is written from what was added, not from what the model already
knows. Both kinds of material are read, because they see different things:
`memory.search` returns passages of the sources, `memory.qa.search` the
questions and answers extracted from them.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from opennotebook import memory
from opennotebook.db.models import Chunk, Source
from opennotebook.db.session import sessionmaker
from opennotebook.memory import qa
from opennotebook.script.grounding import excerpts

# Excerpts kept per requested hit: a slide asking for four hits gets eight
# excerpts, about 7,000 characters; the outline, asking for twelve, gets
# twenty-four.
EXCERPTS_PER_HIT = 2


@dataclass
class Grounding:
    """What the two searches returned for one query."""

    # Passages from the indexed sources.
    passages: list[str] = field(default_factory=list[str])
    # Extracted question and answer pairs.
    qa: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def is_empty(self) -> bool:
        return not self.passages and not self.qa

    def as_context(self) -> str:
        """Both as one block of context for a prompt, labelled so the model
        can tell a source passage from an extracted answer."""
        out: list[str] = [f"SOURCE: {p.strip()}\n\n" for p in self.passages]
        # One line per pair, not `Q:` and `A:` lines: a small model copied
        # those labels straight into the narration ("Bella: Q: What command…").
        out.extend(f"FACT: {a.strip()} (answers: {q.strip()})\n\n" for q, a in self.qa)
        return "".join(out)


@dataclass(frozen=True)
class Scope:
    """Whose collection the script reads."""

    owner: uuid.UUID
    cid: uuid.UUID


async def retrieve(scope: Scope, query: str, top_k: int) -> Grounding:
    """Read both for one query, each in its own rank order. A short session
    of its own: a script is minutes of model calls, and no transaction is
    held open across them."""
    k = max(top_k, 1)
    async with sessionmaker()() as s:
        found = await memory.search(s, scope.owner, scope.cid, query, k)
        ranked = await qa.search(s, scope.owner, scope.cid, query, k)
        documents = [h.text for h in found if h.text.strip()]
        if not documents and not ranked:
            # A query in other words than the sources' — a title such as
            # "Editorial slides", with no embedding model to bridge it —
            # finds nothing by its words. The sources' opening passages
            # stand in, as `excerpts` does for pieces, so the script is
            # still written from the sources and never from nothing.
            rows = await s.scalars(
                select(Chunk.text)
                .join(Source, Source.id == Chunk.source_id)
                .where(Chunk.owner_id == scope.owner, Chunk.collection_id == scope.cid)
                .order_by(Source.created_at, Chunk.ord)
                .limit(k)
            )
            documents = [t for t in rows if t.strip()]
    return Grounding(
        passages=excerpts(documents, query, EXCERPTS_PER_HIT * k),
        qa=[(h.question, h.answer) for h in ranked],
    )
