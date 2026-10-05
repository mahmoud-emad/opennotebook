"""Naming a collection from its sources, and the digest of what a collection
holds that the naming and cover calls read.

Ported from `opennotebook_server/src/collection.rs` (`name`,
`heuristic_title`, `model_title`, `clean_title`, `openings`, `opening`,
`Digest`). Plain async functions with their own database sessions, so they
can move into a background job as they are.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import jobs
from opennotebook.ai import ledger
from opennotebook.ai.client import ai
from opennotebook.ai.errors import AiError
from opennotebook.db.models import Collection, Source
from opennotebook.db.session import sessionmaker
from opennotebook.domain import settings as st

log = logging.getLogger(__name__)

# How long naming a collection may take before the heuristic title is used.
# It runs in the background, so this bounds a task, not a request.
NAME_TIMEOUT = 30.0

# How much of each source the naming call reads: its title and the opening of
# its text. A name says what the sources are about together, and that is in
# their openings, not their bodies.
NAME_OPENING_CHARS = 500
NAME_MAX_SOURCES = 12

# The most of one source read for its opening: enough lines to find 500
# characters of text past the heading, and never a whole large source.
NAME_READ_CHARS = 64 * 1024

# How much of the outputs' titles a cover reads, in characters.
MADE_CHARS = 1500


@dataclass
class Opening:
    """One source as the naming call reads it."""

    title: str
    text: str


@dataclass
class Part:
    """One output as a cover reads it: its kind, title and its parts' names."""

    kind: str
    title: str
    parts: list[str] = field(default_factory=list[str])


@dataclass
class Digest:
    """What a collection holds, bounded, for the models that read it: the
    naming call reads its sources, the cover call all of it."""

    title: str = ""
    sources: list[Opening] = field(default_factory=list[Opening])
    made: list[Part] = field(default_factory=list[Part])

    def sources_text(self) -> str:
        return "\n\n".join(f"Source: {o.title}\n{o.text}" for o in self.sources)

    def made_text(self) -> str:
        """The outputs as lines, cut at `MADE_CHARS`."""
        out = ""
        for p in self.made:
            line = f"{p.kind}: {p.title}"
            if p.parts:
                line += " — " + "; ".join(p.parts)
            room = max(MADE_CHARS - len(out), 0)
            if room < 20:
                break
            out += line[: room - 1] + "\n"
        return out

    def cover_prompt(self) -> str:
        """The cover call's user message."""
        s = ""
        if self.title:
            s += f"Collection title: {self.title}\n\n"
        if self.sources:
            s += "Sources:\n\n" + self.sources_text() + "\n\n"
        if made := self.made_text():
            s += "Made from them:\n" + made
        return s


# ── reading the sources ───────────────────────────────────────────────────────


@dataclass
class Named:
    """A source by the name it is addressed by, with what naming reads."""

    name: str
    title: str
    head: str


async def sources_of(s: AsyncSession, cid: uuid.UUID) -> list[Named]:
    """A collection's sources in name order, as the Rust server listed its
    staged files, each with the start of its text."""
    rows = await s.execute(
        select(Source.name, Source.title, func.left(Source.text, NAME_READ_CHARS)).where(
            Source.collection_id == cid
        )
    )
    return sorted((Named(n, t, h) for n, t, h in rows), key=lambda x: x.name)


def opening(text: str) -> str:
    """The opening of a source, past the heading and the `Source:` or `File:`
    line a page is kept with, which the prompt already carries. Reads lines
    only until it has `NAME_OPENING_CHARS`."""
    words: list[str] = []
    n = 0
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if line.startswith(("# ", "Source: ", "File: ")):
            continue
        for w in line.split():
            n += len(w) + 1
            words.append(w)
        if n > NAME_OPENING_CHARS:
            break
    return " ".join(words)[:NAME_OPENING_CHARS]


def openings(sources: list[Named]) -> list[Opening]:
    """The title and opening of the first `NAME_MAX_SOURCES` sources."""
    read = (Opening(src.title, opening(src.head)) for src in sources[:NAME_MAX_SOURCES])
    return [o for o in read if o.text or o.title]


# ── the name ──────────────────────────────────────────────────────────────────


def clip_title(s: str) -> str:
    """A title clipped on a word: the rule a draft's title has always had."""
    s = s.strip().strip('"').strip()
    if len(s) <= 72:
        return s
    cut = s[:72]
    i = max((i for i, c in enumerate(cut) if c.isspace()), default=-1)
    if i != -1 and len(cut[:i]) >= 24:
        return cut[:i].rstrip()
    return cut.rstrip()


def heuristic_title(read: list[Opening]) -> str:
    """The first source's own title, clipped on a word."""
    return clip_title(read[0].title) if read else ""


def clean_title(raw: str) -> str | None:
    """A model's reply as a title, or None when it is not one.

    Small models wrap a name in quotes, bold, a `Title:` label or a closing
    full stop however plainly they are asked not to; those are taken off. A
    reply that is still not a few words is a sentence about the sources, not
    a name, and the heuristic does better than it.
    """
    line = next((x.strip() for x in raw.splitlines() if x.strip()), None)
    if line is None:
        return None
    line = line.lstrip("#").strip()
    for label in ("Title:", "Name:"):
        if line.startswith(label):
            line = line.removeprefix(label)
            break

    def strip(x: str) -> str:
        return x.strip().rstrip(".!;:,").strip("\"'*“”‘’`").strip()

    # Twice, because the wrappers nest either way round: `"Name".` and `"Name."`.
    line = strip(strip(line))
    return line if 1 <= len(line.split()) <= 10 else None


class NotATitle(ValueError):
    """The model answered, but not with a name."""


async def model_settings(s: AsyncSession, owner: uuid.UUID) -> tuple[str, str]:
    """The model a person's studio calls for small jobs, and the sentence that
    pins its language."""
    model = await st.value(s, owner, st.CHAT_MODEL_KEY)
    language = st.language_rule(await st.value(s, owner, st.LANGUAGE_KEY))
    return model, language


async def model_title(digest: Digest, model: str, language: str) -> str:
    system = (
        "You name a collection of sources a person is studying. Reply with the name only: "
        "3 to 7 words saying what the sources are about together, in title case, with no "
        f"quotes, no trailing punctuation and nothing before or after it. {language}"
    )
    done = await ai().complete(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": digest.sources_text()},
        ],
    )
    title = clean_title(done.text)
    if title is None:
        raise NotATitle(f"the model's name was unusable: {done.text!r}")
    return title


async def name(owner: uuid.UUID, cid: uuid.UUID) -> None:
    """Name a collection from its sources, if the studio still names it and
    the sources changed since it last did.

    The model is asked first and the heuristic answers whenever it cannot: a
    collection with sources is never left untitled because a model was down.
    The result is dropped when the sources changed while the model thought:
    the call that changed them asked for another run, and an older answer
    landing last would name the collection after a set it no longer has.
    """
    async with sessionmaker()() as s:
        # Off in Settings: collections stay untitled until someone names them.
        if not st.is_on(await st.value(s, owner, st.AUTO_NAME_KEY)):
            return
        c = await s.scalar(
            select(Collection).where(Collection.id == cid, Collection.owner_id == owner)
        )
        if c is None:
            return
        sources = await sources_of(s, cid)
        if not sources:
            return
        names = [x.name for x in sources]
        signature = "\n".join(names)
        if not c.title_auto or (c.titled_from == signature and c.title):
            return
        model, language = await model_settings(s, owner)
    digest = Digest(sources=openings(sources))
    fallback = heuristic_title(digest.sources)
    try:
        async with (
            asyncio.timeout(NAME_TIMEOUT),
            ledger.spending(owner, "title", collection_id=cid),
        ):
            title = await model_title(digest, model, language)
    except (AiError, NotATitle, TimeoutError) as e:
        log.info("collection %s: naming from the heuristic: %s", cid, e)
        title = fallback
    if not title:
        return
    # Re-read under the lock right before the write: the person may have typed
    # a title, or deleted the collection, or changed its sources while the
    # model was asked.
    async with sessionmaker()() as s, s.begin():
        now = await s.scalar(
            select(Collection)
            .where(Collection.id == cid, Collection.owner_id == owner)
            .with_for_update()
        )
        if now is None or not now.title_auto:
            return
        if [x.name for x in await sources_of(s, cid)] != names:
            return
        now.title = title
        now.titled_from = signature
        await jobs.announce(s, cid)
