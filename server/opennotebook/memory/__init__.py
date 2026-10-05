"""Retrieval over a collection's sources. A port of `opennotebook_memory`.

Every source is split into passages of roughly a paragraph or two
(Markdown-aware, so a passage does not start mid-heading) and indexed for
full-text search (a generated `tsvector` with GIN) and, when an embedding
model is set, as vectors (pgvector). `search` fuses the two rankings with
reciprocal rank fusion, so its scores are on one scale and higher is better.

Without an embedding model everything still works on full-text search alone,
which keeps tests hermetic and lets the studio run against an endpoint that
offers no embedding model.
"""

import asyncio
import math
import re
import uuid
from dataclasses import dataclass

from semantic_text_splitter import MarkdownSplitter
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.ai.client import ai
from opennotebook.config import settings
from opennotebook.db.models import Chunk, Source

# Passage size, in characters: a range so a split falls on a paragraph or
# sentence boundary rather than at an exact count.
CHUNK_CHARS = (600, 1200)

# Candidates each ranking contributes before fusion.
CANDIDATES_PER_HIT = 4

# Reciprocal rank fusion's damping constant; 60 is the value from the
# original paper and the usual default.
RRF_K = 60.0

_splitter = MarkdownSplitter(CHUNK_CHARS)


@dataclass
class Hit:
    """A passage that matched a search."""

    source: str  # the source's name
    ord: int
    start: int
    end: int
    text: str
    # Fused rank score: higher is better, comparable only within one search.
    score: float


def split(markdown: str) -> list[tuple[int, str]]:
    """A source's passages with their offsets, blank ones dropped."""
    return [(start, t) for start, t in _splitter.chunk_indices(markdown) if t.strip()]


def ts_query(query: str) -> str | None:
    """A tsquery matching any of the query's words, or None when it has none
    worth matching. Only letters and digits get through, so nothing in it is
    read as query syntax."""
    words = [w.lower() for w in re.split(r"[^\w]+|_", query) if len(w) >= 2]
    return " | ".join(dict.fromkeys(words)) if words else None


def normalized(v: list[float]) -> list[float]:
    """Scaled to unit length, so cosine similarity is a dot product."""
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else v


def fuse(lists: list[list[uuid.UUID]], k: int) -> list[tuple[uuid.UUID, float]]:
    """Reciprocal rank fusion of several best-first lists: the top `k` ids
    with their fused scores, best first. Ties go to the id seen first."""
    score: dict[uuid.UUID, list[float]] = {}
    for lst in lists:
        for rank, id_ in enumerate(lst):
            e = score.setdefault(id_, [0.0, float(len(score))])
            e[0] += 1.0 / (RRF_K + rank + 1.0)
    out = sorted(score.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:k]
    return [(id_, s) for id_, (s, _) in out]


def embed_model() -> str:
    """The embedding model whoever runs the studio set
    (OPENNOTEBOOK_EMBED_MODEL); empty means full-text search only."""
    return settings().embed_model.strip()


async def _embed(model: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        out.extend(normalized(v) for v in await ai().embed(model, texts[i : i + 64]))
    return out


@dataclass
class Passages:
    """A text split into passages and, with an embedding model, embedded:
    ready to store, with nothing slow left to do."""

    model: str
    rows: list[tuple[int, str]]
    vectors: list[list[float]] | None


async def passages(text_: str, model: str | None = None) -> Passages:
    """Split and embed a source's text. Call it outside any transaction:
    embedding is a model call, and splitting a large document is seconds of
    work, done off the event loop."""
    model = embed_model() if model is None else model
    rows = await asyncio.to_thread(split, text_)
    vectors = await _embed(model, [t for _, t in rows]) if model and rows else None
    return Passages(model, rows, vectors)


async def store(s: AsyncSession, src: Source, p: Passages) -> int:
    """Store one source's passages, replacing what was there. Returns how
    many passages were stored."""
    await s.execute(delete(Chunk).where(Chunk.source_id == src.id))
    for i, (start, t) in enumerate(p.rows):
        s.add(
            Chunk(
                owner_id=src.owner_id,
                collection_id=src.collection_id,
                source_id=src.id,
                ord=i,
                start=start,
                end=start + len(t),
                text=t,
                embed_model=p.model or None,
                embedding=p.vectors[i] if p.vectors else None,
            )
        )
    await s.flush()
    return len(p.rows)


async def search(
    s: AsyncSession,
    owner: uuid.UUID,
    cid: uuid.UUID,
    query: str,
    top_k: int,
    *,
    sources: list[str] | None = None,
    model: str | None = None,
) -> list[Hit]:
    """The passages most about `query`, best first, from the collection's
    sources (or only the named ones)."""
    model = embed_model() if model is None else model
    n = max(top_k, 1) * CANDIDATES_PER_HIT
    scope = "c.owner_id = :owner AND c.collection_id = :cid"
    params: dict[str, object] = {"owner": owner, "cid": cid, "n": n}
    if sources is not None:
        scope += " AND src.name = ANY(:names)"
        params["names"] = sources
    lists: list[list[uuid.UUID]] = []
    if q := ts_query(query):
        rows = await s.execute(
            text(
                f"""SELECT c.id FROM chunks c JOIN sources src ON src.id = c.source_id
                    WHERE {scope} AND c.tsv @@ to_tsquery('english', :q)
                    ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', :q)) DESC, c.id
                    LIMIT :n"""
            ),
            {**params, "q": q},
        )
        lists.append([r[0] for r in rows])
    if model:
        (qv,) = await _embed(model, [query])
        rows = await s.execute(
            text(
                f"""SELECT c.id FROM chunks c JOIN sources src ON src.id = c.source_id
                    WHERE {scope} AND c.embed_model = :model AND c.embedding IS NOT NULL
                      AND vector_dims(c.embedding) = :dims
                    ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :n"""
            ),
            {**params, "model": model, "dims": len(qv), "qv": str(qv)},
        )
        lists.append([r[0] for r in rows])
    fused = fuse(lists, top_k)
    if not fused:
        return []
    found = {
        c.id: (c, name)
        for c, name in await s.execute(
            select(Chunk, Source.name)
            .join(Source, Source.id == Chunk.source_id)
            .where(Chunk.id.in_([i for i, _ in fused]))
        )
    }
    return [
        Hit(name, c.ord, c.start, c.end, c.text, score)
        for i, score in fused
        if i in found
        for c, name in [found[i]]
    ]
