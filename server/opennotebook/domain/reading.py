"""A collection's sources as documents to read whole: what ask, mind maps and
study notes are made from. A port of `read_docs` in the Rust server's
`sources_impl.rs`, and of the price of one such call (`estimate_live.rs`).
"""

import uuid
from collections.abc import Callable

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.ai import client
from opennotebook.db.models import Source
from opennotebook.errors import Problem, readable
from opennotebook.script.mindmap import NamedDoc


async def read_docs(
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, names: list[str] | None
) -> list[NamedDoc]:
    """The collection's sources named, or every source when none are, oldest
    first. A name that is not one of its sources is refused rather than
    skipped, because an agent passing a stale name should hear about it."""
    rows = list(
        await s.scalars(
            select(Source)
            .where(Source.collection_id == cid, Source.owner_id == owner)
            .order_by(Source.created_at, Source.name)
        )
    )
    if names:
        by_name = {r.name: r for r in rows}
        if missing := next((n for n in names if n not in by_name), None):
            raise Problem(
                422,
                f"“{missing}” is not a source of this collection. "
                "List the collection's sources to see their names, then try again.",
            )
        rows = [by_name[n] for n in dict.fromkeys(names)]
    if not rows:
        raise Problem(409, readable("no sources"))
    docs = [
        NamedDoc(name=r.name, title=r.title.strip() or r.name, text=r.text, url=r.url)
        for r in rows
        if r.text.strip()
    ]
    if not docs:
        raise Problem(
            409,
            "The sources chosen have no readable text. Add a page or a note with text, "
            "then try again.",
        )
    return docs


class Estimate(BaseModel):
    """What one call over a collection's sources would cost, before making it."""

    sources: int
    chars: int = Field(description="Characters of source text")
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float = Field(description="The typical cost; 0 when the model has no price")
    cost_high_usd: float = Field(description="Twice the typical cost: a second attempt")
    priced: bool = Field(description="False when the model's price is not known")


async def estimate(
    docs: list[NamedDoc], model: str, tokens: Callable[[int], tuple[int, int]]
) -> Estimate:
    """The price of one call over `docs` on `model`, `tokens` giving the
    tokens in and out for so many characters of text. The high end is two
    calls, because a thin first answer is asked for once more."""
    chars = sum(len(d.text) for d in docs)
    input_tokens, output_tokens = tokens(chars)
    price = await client.ai().catalogue.price(model)
    one = price.cost(input_tokens, output_tokens) if price else 0.0
    return Estimate(
        sources=len(docs),
        chars=chars,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=one,
        cost_high_usd=2 * one,
        priced=price is not None,
    )
