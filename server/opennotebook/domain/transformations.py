"""Transformations: named instructions to run over a collection's sources.
The built-in ones are here; a person's own are rows, one per name."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.db.models import Transformation
from opennotebook.errors import Problem, not_found

NAME_MAX = 60
PROMPT_MAX = 2_000
# How many of their own a person may keep.
CUSTOM_MAX = 50


@dataclass(frozen=True)
class Found:
    """A transformation to run: built in, or a person's own."""

    id: str
    name: str
    description: str
    prompt: str
    builtin: bool


BUILTINS: list[Found] = [
    Found(
        "summary",
        "Summary",
        "What the sources are about and their main points",
        "Summarize the sources: what they are about and their main points, in a few short "
        "paragraphs.",
        True,
    ),
    Found(
        "key-claims",
        "Key claims",
        "The claims the sources make, most important first",
        "List the key claims the sources make, one bullet each, the most important first.",
        True,
    ),
    Found(
        "flashcards",
        "Flashcards",
        "Question and answer cards to study from",
        "Write ten flashcards that help someone learn this material. For each, a line "
        "starting **Q:** with the question and a line starting **A:** with the answer.",
        True,
    ),
    Found(
        "timeline",
        "Timeline",
        "Dated events, oldest first",
        "Build a timeline of the dated events the sources mention, oldest first, one line "
        "each as: date, then the event. Leave out anything with no date.",
        True,
    ),
    Found(
        "open-questions",
        "Open questions",
        "What the sources leave unanswered or disagree on",
        "List the questions the sources raise but do not answer, and the points where they "
        "disagree with each other, one bullet each.",
        True,
    ),
]


def builtin(key: str) -> Found | None:
    return next((b for b in BUILTINS if b.id == key), None)


def clean(name: str, prompt: str) -> tuple[str, str]:
    """A name and instruction as they are kept, or why they cannot be."""
    name, prompt = " ".join(name.split()), prompt.strip()
    if not name:
        raise Problem(422, "The transformation has no name. Give it a short name, then save.")
    if len(name) > NAME_MAX:
        raise Problem(422, f"The name is too long. Keep it to {NAME_MAX} characters or fewer.")
    if not prompt:
        raise Problem(422, "The instruction is empty. Say what to do with the sources, then save.")
    if len(prompt) > PROMPT_MAX:
        raise Problem(
            422, f"The instruction is too long. Keep it to {PROMPT_MAX} characters or fewer."
        )
    if any(b.name.lower() == name.lower() for b in BUILTINS):
        raise Problem(422, f"“{name}” is the name of a built-in transformation. Pick another.")
    return name, prompt


def of(row: Transformation) -> Found:
    return Found(str(row.id), row.name, "", row.prompt, False)


async def mine(s: AsyncSession, owner: uuid.UUID) -> list[Transformation]:
    rows = await s.scalars(
        select(Transformation)
        .where(Transformation.owner_id == owner)
        .order_by(Transformation.created_at, Transformation.id)
    )
    return list(rows)


async def listed(s: AsyncSession, owner: uuid.UUID) -> list[Found]:
    """The built-in transformations, then the person's own."""
    return [*BUILTINS, *(of(r) for r in await mine(s, owner))]


async def one(s: AsyncSession, owner: uuid.UUID, key: str) -> Transformation:
    """One of the person's own, by id."""
    try:
        tid = uuid.UUID(key)
    except ValueError:
        raise not_found("That transformation") from None
    row = await s.scalar(
        select(Transformation).where(Transformation.id == tid, Transformation.owner_id == owner)
    )
    if row is None:
        raise not_found("That transformation")
    return row


async def find(s: AsyncSession, owner: uuid.UUID, ref: str) -> Found:
    """A transformation by id, key or name (any case), built in or the
    person's own. A name that is none of them says what there is."""
    ref = " ".join(ref.split())
    every = await listed(s, owner)
    low = ref.lower()
    for f in every:
        if low in (f.id.lower(), f.name.lower()):
            return f
    raise Problem(
        422,
        f"There is no transformation called “{ref}”. "
        f"The ones there are: {', '.join(f.name for f in every)}.",
    )


def split(every: list[Found], arg: str) -> tuple[Found | None, str]:
    """What follows `/transform` as the transformation it names and the focus
    after it. A name may be several words, so the longest one the text opens
    with wins; the key (`key-claims`) works as the name does."""
    text = " ".join(arg.split())
    low = text.lower()
    best: Found | None = None
    used = 0
    for f in every:
        for label in (f.name.lower(), f.id.lower()):
            if (low == label or low.startswith(label + " ")) and len(label) > used:
                best, used = f, len(label)
    return (None, text) if best is None else (best, text[used:].strip())


async def count(s: AsyncSession, owner: uuid.UUID) -> int:
    return await s.scalar(select(func.count()).where(Transformation.owner_id == owner)) or 0
