"""The Ask chat, as an agent that does the work it talks about. A port of
`opennotebook_server/src/agent.rs`.

# What it replaced

The chat used to be a model asked for two sentences of JSON and forbidden to
claim it could do anything. With web research on, it also declared a session
buildable the moment a person named a topic — so "help me understand how
computers work" put a Start building button on screen beside a reply asking
what exactly they wanted to know. Nothing had been read and nothing had been
found; the button promised a session built from nothing.

Now the model has tools and uses them: it searches the web, reads the pages
worth reading into the collection's sources, can run a deeper web research
pass, answers from the sources with citations, and makes things itself. A
build is started here, on the server, by `start_build`: the web app is only a
client and is told about it as a step and a `build` event.

# How the work is shown

The way Claude Code shows an agent working, because it is the pattern people
already read: every action is one line naming what is being done and to
what — "Searching the web · history of computing" — with a spinner while it
runs and a result line under it when it finishes ("⎿ 8 results"). Long
actions report progress on that result line rather than adding lines. The
reply comes after the work, short, and does not repeat what the lines
already said.

A turn is a stream of events, one JSON object each:

| `t`         | fields                         | meaning                                         |
|-------------|--------------------------------|-------------------------------------------------|
| `thinking`  |                                | the model is deciding what to do next           |
| `step`      | `id`, `kind`, `text`, `detail` | an action started                               |
| `step_note` | `id`, `text`                   | progress on a running action                    |
| `step_done` | `id`, `ok`, `text`             | the action finished, with its result            |
| `source`    | `src`                          | a source was added to the collection            |
| `reply`     | `text`, `citations`?           | what the agent says                             |
| `build`     | `kind`, `title`, `id`          | an output was started (slides, audio) or made   |
| `state`     | `ready`, `title`               | whether there are sources, and the title        |

# Where it runs

In the request. A turn is seconds, or about a minute with deep research, and
the person is watching it happen. What it produces is not held in the
request: every source is committed the moment it is read, so a closed tab
loses nothing the build needs.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import func, select

from opennotebook import research
from opennotebook.ai import client, ledger
from opennotebook.ai.errors import AiError, Kind
from opennotebook.build import pipeline
from opennotebook.db.models import Collection, Source, User
from opennotebook.db.session import sessionmaker, unit
from opennotebook.domain import collections, reading, refresh, sources
from opennotebook.domain import settings as st
from opennotebook.errors import Problem
from opennotebook.script import cite
from opennotebook.script.errors import ScriptError

log = logging.getLogger(__name__)

Event = dict[str, Any]

# How many think-act rounds one turn may take before it must answer.
MAX_ROUNDS = 8

# The most pages one `add_sources` call reads.
MAX_FETCH = 4

# How many replacement pages one add_sources call may try.
MAX_REPLACE = 6

# Turns of the conversation the model is given.
HISTORY = 24

# Prose long enough to be material rather than conversation.
#
# Somebody pasting six paragraphs into the chat box means them to be read;
# somebody typing "go on" does not. A threshold is a guess, but the failure it
# replaces is not a guess: without one, typed notes are invited by the
# greeting and then silently dropped.
NOTE_CHARS = 240

# What the studio makes, as start_build names them, with the wire name the
# page knows each by and how the agent describes it.
KINDS: list[tuple[str, str, str]] = [
    (
        "slides",
        "session",
        "narrated slides: a slide deck with a spoken script, watched like a lesson and "
        "interrupted with spoken questions (a few minutes to build)",
    ),
    (
        "audio",
        "audio",
        "an audio overview: a spoken conversation about the sources to listen to, which can "
        "be joined with spoken questions (a few minutes to build)",
    ),
    (
        "mindmap",
        "mindmap",
        "a mind map: the sources' topics as a tree; clicking a topic asks about it (seconds)",
    ),
    (
        "notes",
        "notes",
        "study notes: the key ideas, a quiz and a glossary, every claim cited (under a minute)",
    ),
]

# How each kind is named in a sentence, by its wire name.
LABELS = {
    "session": "narrated slides",
    "audio": "an audio overview",
    "mindmap": "a mind map",
    "notes": "study notes",
}

NO_KEY = (
    "The studio has no AI key yet, so I cannot answer. Add OPENNOTEBOOK_AI_KEY to the "
    "server's environment; links you paste here are still read into the sources."
)
OUT_OF_CREDIT = (
    "The AI account behind the studio is out of credit, so I cannot search or write right "
    "now. Whatever I read is on the left; once credit is added, ask again."
)
LOST_MODEL = (
    "I lost the model in the middle of that. Whatever I read is on the left; ask again, "
    "or build from it."
)
FOUND_SO_FAR = (
    "That is what I found so far — it is on the left. Say build when you are happy with "
    "it, or tell me what is missing."
)
MAKING_IT = (
    "Making it now — it shows up in the Studio tab. You can watch it there, or leave; "
    "it keeps going."
)
MADE_IT = "Done — it opens beside the chat, and it is in the Studio tab."


def default_kind(output: str) -> str:
    """The start_build kind a page's `output` stands for: slides when it
    names nothing the studio makes."""
    return next((k[0] for k in KINDS if k[1] == output), "slides")


def wire_kind(kind: str, output: str) -> str:
    """The wire name of a start_build kind, falling back to what the page
    picked."""
    kind = kind or default_kind(output)
    return next((k[1] for k in KINDS if k[0] == kind), "session")


def asked_to_build(message: str) -> bool:
    """Whether the person asked for something to be made, in this message.

    "Help me understand the Linux kernel and build the session once you
    collect the resources" asks for it; "don't build yet" does not."""
    m = message.lower()
    wants = any(
        w in m
        for w in (
            "build",
            "generate",
            "make the session",
            "create the session",
            "start the session",
            "make a mind map",
            "make study notes",
            "make an audio",
            "make slides",
        )
    )
    holds = any(
        w in m
        for w in (
            "don't build",
            "do not build",
            "dont build",
            "not build yet",
            "before build",
            "before you build",
            "wait",
        )
    )
    return wants and not holds


_REASONING_TAGS = ("thinking", "think", "reasoning", "reflection")


def without_reasoning(text: str) -> str:
    """`text` without the model's reasoning.

    Some chat models write their deliberation into the reply itself, wrapped
    in `<thinking>` or `<think>` tags. Nothing removed it, so a person saw
    "I'll now list only the pages that were successfully added…" in a
    thinking block above the actual answer. Closed blocks go wherever they
    are; an opening tag that is never closed takes the rest of its paragraph
    with it, because a reply cut off mid-reasoning has nothing after the tag
    worth showing."""
    out = text
    for tag in _REASONING_TAGS:
        open_, close = f"<{tag}>", f"</{tag}>"
        while (start := out.lower().find(open_)) >= 0:
            lower = out.lower()
            i = lower.find(close, start)
            if i >= 0:
                end = i + len(close)
            else:
                para = lower.find("\n\n", start)
                end = para if para >= 0 else len(out)
            out = out[:start] + out[end:]
        # A stray closing tag left by a block that opened in an earlier turn.
        while (i := out.lower().find(close)) >= 0:
            out = out[:i] + out[i + len(close) :]
    return out.strip()


def without_failed(text: str, failed: Sequence[str]) -> str:
    """`text` without any line that names one of `failed`. The last guard
    after the model has been asked once to leave them out."""
    if not failed:
        return text
    return "\n".join(ln for ln in text.splitlines() if not any(u in ln for u in failed)).strip()


def next_replacement(pool: Sequence[str], tried: set[str], dead_hosts: set[str]) -> str | None:
    """The next search result not yet tried, on a host that has not failed."""
    return next(
        (u for u in pool if u not in tried and research.short_host(u) not in dead_hosts), None
    )


def split_material(text: str) -> tuple[list[str], str]:
    """The links in a message, and everything that is not a link.

    Whitespace-separated, because that is how a person pastes: the link is a
    token or it is prose about a link. The punctuation around it is trimmed
    before the token is judged — "see (https://x.dev/a)." is a sentence with
    a link in it, not a page called `a.` and not prose with no link at all."""
    urls: list[str] = []
    rest: list[str] = []
    for token in text.split():
        bare = token.lstrip("([{<\"'").rstrip(".,;:)]}>\"'")
        if bare.startswith(("http://", "https://")):
            urls.append(bare)
        else:
            rest.append(token)
    return urls, " ".join(rest)


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def tools(web: bool) -> list[dict[str, Any]]:
    """The tools the agent has. Without web research it can still read links
    the person names and make things from them; it cannot go looking."""
    out: list[dict[str, Any]] = []
    if web:
        out.append(
            _tool(
                "web_search",
                "Search the web. Returns results with title, url and snippet. Use it to find "
                "the best pages on the person's topic before adding them.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "A focused search query"}
                    },
                    "required": ["query"],
                },
            )
        )
        out.append(
            _tool(
                "deep_research",
                "Run a deeper web research pass (one to a few minutes, by the studio's "
                "research depth setting) that reads many pages and writes a report, added as "
                "one source. Use it for broad topics where a few pages are not enough.",
                {
                    "type": "object",
                    "properties": {"topic": {"type": "string"}},
                    "required": ["topic"],
                },
            )
        )
    out.append(
        _tool(
            "add_sources",
            f"Read up to {MAX_FETCH} web pages and add them to the collection's sources. "
            "Everything the studio makes is made from the sources.",
            {
                "type": "object",
                "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
                "required": ["urls"],
            },
        )
    )
    out.append(
        _tool(
            "ask_sources",
            "Answer a question from the sources already added, with citations. The answer is "
            "shown to the person directly, so do not repeat it. Use it whenever they ask what "
            "their sources or material say.",
            {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question, in the person's words",
                    }
                },
                "required": ["question"],
            },
        )
    )
    out.append(
        _tool(
            "start_build",
            "Make one of the studio's outputs from the sources: narrated slides, an audio "
            "overview, a mind map or study notes. It appears in the Studio tab. Refused when "
            "there are no sources.",
            {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [k[0] for k in KINDS],
                        "description": "What to make. Leave it out for what the person has "
                        "picked on the page.",
                    },
                    "title": {"type": "string", "description": "A short title for it"},
                },
                "required": ["title"],
            },
        )
    )
    return out


def system_prompt(
    sources_now: Sequence[str],
    web: bool,
    output: str,
    language_rule: str,
    read_only: bool = False,
) -> str:
    listed = ", ".join(sources_now) if sources_now else "none yet"
    if web:
        web_rule = (
            "Web research is ON. When the person names a topic, find the material yourself: "
            "web_search, then ALWAYS add_sources with the 2 to 4 best pages from the results "
            "before you reply — a page you only mention is not a source and cannot be built "
            "from (prefer authoritative ones: encyclopedias, the paper's own page, official "
            "documentation, well-known explainers). Use deep_research only for a broad topic "
            "where a few pages will not cover it. Never ask the person to paste something you "
            'can search for. If a name is ambiguous ("the Mochi paper" could be several '
            "things), search first and pick the reading that fits, or ask one short question "
            "if the results really split."
        )
        research_line = (
            "- find sources: search the web (web_search) and add the best pages (add_sources)\n"
            "- research in depth: a longer pass over many pages, written up as one source "
            "(deep_research)\n"
        )
    else:
        web_rule = (
            "Web research is OFF. You cannot search. Read any link the person names with "
            "add_sources, and otherwise ask them for a link or notes."
        )
        research_line = ""
    can_make = "\n".join(f"- {name}: {what}" for name, _, what in KINDS)
    picked = default_kind(output)
    p = (
        "You are the OpenNotebook agent. The studio turns a person's sources (web pages, "
        "PDFs, documents, notes) into things that help them learn, and everything it makes "
        "is made only from those sources.\n\n"
        "What you can do:\n"
        f"{research_line}"
        "- read a link the person gives you into the sources (add_sources)\n"
        "- answer questions from the sources, with citations (ask_sources)\n"
        f"- make any of these from the sources with start_build:\n{can_make}\n\n"
        "When the person asks what you can do, say this plainly and briefly, mention that "
        "they can add their own files and links on the left, and that typing / in the chat "
        "lists these as commands (/slides, /audio, /mindmap, /notes, /search, /research, "
        "/ask, /help). A message starting with /search means find sources on what follows; "
        "/research means research what follows in depth.\n\n"
        f"{web_rule}\n\n"
        f"Sources already added: {listed}\n\n"
        f"Making things: the person has picked {picked} on the page, so that is what "
        "start_build makes unless they ask for another kind. When there are sources that "
        "cover what they want, either call start_build — but only if they have asked you to "
        "build, make, create or generate something — or tell them in one sentence what you "
        "gathered and what you can make from it. start_build is refused with no sources.\n\n"
        "Questions: when the person asks what their sources say about something, call "
        "ask_sources rather than answering yourself. Its answer reaches them directly with "
        "citations.\n\n"
        "The person sees every tool you use as a line in the chat and every added source on "
        "the left, so do not describe your searches or repeat the list of pages. Reply in two "
        "or three short sentences, or a short list when asked what you can do; you may use "
        "Markdown (bold, a link, a list) sparingly. Only ever call a page a source if "
        "add_sources reported it as added — a page that could not be read is NOT a source, so "
        "never name it as one. Never invent what a source says."
    )
    if read_only:
        p += (
            "\n\nThis collection is a read-only copy of someone else's. Nothing can be added "
            "to it and nothing can be made in it: add_sources, deep_research and start_build "
            "are refused. Answer from its sources with ask_sources, and when the person wants "
            "to add or make something, tell them to start a new collection of their own."
        )
    if language_rule:
        p += f"\n\n{language_rule}"
    return p


# ── the turn ─────────────────────────────────────────────────────────────────


@dataclass
class Picks:
    """What the page has picked, for what a turn makes when the person does
    not say: the kind ("session", "audio", "mindmap", "notes"; empty for
    slides), and a deck's style or an audio overview's format and length."""

    output: str = ""
    style: str | None = None
    audio_format: str | None = None
    audio_length: str | None = None


@dataclass
class Turn:
    """Who is asking, about which collection, with what picked."""

    me: User
    cid: uuid.UUID
    picks: Picks = field(default_factory=Picks)
    # The "Research the web" switch: off, the agent has no web tools and
    # reads only what the person gives it.
    web: bool = True

    @property
    def owner(self) -> uuid.UUID:
        return self.me.id


@dataclass
class Fetched:
    """One thing asked to be read into the sources, and how it went."""

    url: str
    ok: bool
    title: str = ""
    chars: int = 0
    error: str = ""
    name: str = ""

    @classmethod
    def of(cls, src: Source, url: str = "") -> Fetched:
        return cls(url=url or src.url, ok=True, title=src.title, chars=src.chars, name=src.name)

    def as_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "title": self.title,
            "chars": self.chars,
            "error": self.error,
            "name": self.name,
            "icon": "",
        }


class Ids:
    """Step ids for one turn: a1, a2, …"""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"a{self.n}"


def step(id: str, kind: str, text: str, detail: str = "") -> Event:
    return {"t": "step", "id": id, "kind": kind, "text": text, "detail": detail}


def done(id: str, ok: bool, text: str) -> Event:
    return {"t": "step_done", "id": id, "ok": ok, "text": text}


def reply(text: str, citations: list[dict[str, Any]] | None = None) -> Event:
    e: Event = {"t": "reply", "text": text}
    if citations is not None:
        e["citations"] = citations
    return e


def fetch_started(id: str, url: str) -> Event:
    """The line for a page about to be read."""
    return step(id, "fetch", "Reading", research.short_host(url))


def fetched_events(id: str, f: Fetched) -> list[Event]:
    """A finished read as the events that show it: its result line, and the
    source row. The step line itself went out before the read started."""
    if not f.ok:
        # Only a page that was read becomes a source. A failed one stays as
        # its step line in the chat, which says what happened, and is left
        # off the list the build is made from.
        return [done(id, False, f.error)]
    return [
        done(id, True, f"added “{f.title}” · {f.chars // 6} words"),
        {"t": "source", "src": f.as_json()},
    ]


# ── what the tools do to the collection ─────────────────────────────────────


async def refusal(t: Turn) -> str | None:
    """Why nothing may be added to or made in this collection, or None when
    it may: a read-only copy refuses, through `collections.editable`."""
    async with sessionmaker()() as s:
        try:
            await collections.editable(s, t.owner, t.cid)
        except Problem as e:
            return e.detail
    return None


async def source_names(t: Turn) -> list[str]:
    async with sessionmaker()() as s:
        return list(
            await s.scalars(
                select(Source.name)
                .where(Source.collection_id == t.cid, Source.owner_id == t.owner)
                .order_by(Source.created_at, Source.name)
            )
        )


async def source_count(t: Turn) -> int:
    async with sessionmaker()() as s:
        n = await s.scalar(
            select(func.count())
            .select_from(Source)
            .where(Source.collection_id == t.cid, Source.owner_id == t.owner)
        )
    return n or 0


async def fetch_one(t: Turn, http: httpx.AsyncClient, url: str) -> Fetched:
    """Read one page into the collection, committed at once, so a closed tab
    loses nothing that was read. Read with no transaction open; kept in a
    short one."""
    try:
        ready = await sources.prepare_page(http, url)
        async with sessionmaker()() as s, s.begin():
            src = await sources.keep(s, t.owner, t.cid, ready)
            return Fetched.of(src, url)
    except sources.Refused as e:
        return Fetched(url=url, ok=False, error=str(e))
    except Problem as e:
        return Fetched(url=url, ok=False, error=e.detail)


async def fetch_urls(t: Turn, urls: Sequence[str]) -> list[Fetched]:
    """One result per url, in order, failures included: a source that did
    not arrive must be visible, because a deck built without it looks exactly
    like a deck built with it. A read-only copy refuses every one."""
    if why := await refusal(t):
        return [Fetched(url=u, ok=False, error=why) for u in urls]
    async with sources.http_client() as http:
        return [await fetch_one(t, http, u) for u in urls]


async def keep_note(t: Turn, text: str) -> Fetched:
    """Keep a long paste as a note source. Somebody pasting six paragraphs
    means them to be read."""
    if why := await refusal(t):
        return Fetched(url="", ok=False, title="your note", error=why)
    try:
        ready = await sources.prepare_note(text)
        async with sessionmaker()() as s, s.begin():
            src = await sources.keep(s, t.owner, t.cid, ready)
            return Fetched.of(src)
    except (sources.Refused, Problem) as e:
        why = e.detail if isinstance(e, Problem) else str(e)
        return Fetched(url="", ok=False, title="your note", error=why)


async def ask(
    t: Turn, ids: Ids, values: dict[str, str], question: str
) -> AsyncIterator[Event | str]:
    """A question answered from the sources, with citations: its step, then
    the reply, whole. Yields, last, a plain string: what the model is told.

    The answer goes to the person as it is, citations and all, and ends the
    turn: handed back to the model it would come out paraphrased, and a
    paraphrase cannot keep a citation honest."""
    id = ids()
    yield step(id, "read", "Reading your sources", question)
    try:
        async with sessionmaker()() as s:
            docs = await reading.read_docs(s, t.owner, t.cid, None)
        async with ledger.spending(t.owner, "ask", collection_id=t.cid):
            a = await cite.answer(
                docs,
                question,
                model=values[st.CHAT_MODEL_KEY],
                language_rule=st.language_rule(values[st.LANGUAGE_KEY]),
            )
    except Problem as e:
        yield done(id, False, e.detail)
        yield f"asking the sources failed: {e.detail}"
        return
    except (AiError, ScriptError) as e:
        yield done(id, False, e.sentence)
        yield f"asking the sources failed: {e.sentence}"
        return
    n = len(a.cited)
    yield done(
        id,
        True,
        "no passage cited" if n == 0 else "1 passage cited" if n == 1 else f"{n} passages cited",
    )
    yield reply(
        a.text,
        [
            {
                "n": c.n,
                "name": docs[c.doc].name,
                "title": docs[c.doc].title,
                "url": docs[c.doc].url,
                "excerpt": c.excerpt,
            }
            for c in a.cited
        ],
    )
    yield "the answer was shown to the person"


_MAKING = {
    "session": "Starting narrated slides",
    "audio": "Starting an audio overview",
    "mindmap": "Making a mind map",
    "notes": "Writing study notes",
}


async def make(t: Turn, ids: Ids, kind: str, title: str, focus: str) -> AsyncIterator[Event]:
    """Make one output, by its wire name, here on the server: its step, its
    result, and a `build` event naming what was made. A deck or an audio
    overview is started as a job and followed in the Studio tab; a map or
    notes take seconds and are made while the step runs. A read-only copy
    refuses, and the step says so."""
    # Late: the routes import the chat, and the chat makes through them.
    from opennotebook.api import mindmaps, notes, sessions

    id = ids()
    yield step(id, "build", _MAKING.get(kind, "Making it"), title or focus)
    if why := await refusal(t):
        yield done(id, False, why)
        return
    p = t.picks
    try:
        # A unit of work, as a request has: the routes let their transaction
        # go while the model writes, and write in a new one after.
        async with unit() as s:
            if kind in ("session", "audio"):
                audio = kind == "audio"
                req = sessions.BuildReq.model_validate(
                    {
                        "kind": "audio" if audio else "slides",
                        "title": title[:200],
                        "style": None if audio else p.style,
                        "audio_format": p.audio_format if audio else None,
                        "audio_length": p.audio_length if audio else None,
                        "focus": focus[:400] if audio else "",
                    }
                )
                o = await sessions.build(t.cid, req, s, t.me)
                made = (str(o.id), o.title, "started · follow it in the Studio tab")
            elif kind == "mindmap":
                req = mindmaps.MakeReq(focus=focus[:400])
                # Made while the turn waits, so the turn can say what it made.
                m = await mindmaps.make_mindmap_now(t.cid, req, s, t.me)
                made = (str(m.id), m.title, f"{m.node_count} topics")
            else:
                n = await notes.make_notes_now(t.cid, mindmaps.MakeReq(focus=focus[:400]), s, t.me)
                made = (str(n.id), n.title, f"{n.ideas} key ideas, {n.questions} questions")
    except Problem as e:
        yield done(id, False, e.detail)
        return
    yield done(id, True, made[2])
    yield {"t": "build", "kind": kind, "title": made[1], "id": made[0]}


async def _with_notes(
    id: str, work: asyncio.Task[research.Found], notes: asyncio.Queue[str]
) -> AsyncIterator[Event]:
    """The progress `work` reports, as notes on step `id`, until it ends."""
    try:
        while not work.done():
            getter = asyncio.ensure_future(notes.get())
            await asyncio.wait({work, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                yield {"t": "step_note", "id": id, "text": getter.result()}
            else:
                getter.cancel()
        while not notes.empty():
            yield {"t": "step_note", "id": id, "text": notes.get_nowait()}
    finally:
        if not work.done():
            work.cancel()


async def deep_research(
    t: Turn, ids: Ids, values: dict[str, str], topic: str
) -> AsyncIterator[Event | str]:
    """A research pass on the web, written up and added as one source. Its
    progress is shown on the step's result line. Yields, last, what the
    model is told."""
    id = ids()
    yield step(id, "research", "Researching in depth", topic)
    if why := await refusal(t):
        yield done(id, False, why)
        yield f"refused: {why}"
        return
    notes: asyncio.Queue[str] = asyncio.Queue()

    async def say(m: str) -> None:
        notes.put_nowait(m)

    async def run() -> research.Found:
        async with ledger.spending(t.owner, "research", collection_id=t.cid):
            return await research.gather(values, topic, say)

    work = asyncio.ensure_future(run())
    async for e in _with_notes(id, work, notes):
        yield e
    try:
        found = work.result()
        src = await pipeline.add_research(t.owner, t.cid, found)
    except (research.ResearchError, AiError) as e:
        yield done(id, False, e.sentence)
        yield f"research failed: {e.sentence}"
        return
    except Problem as e:
        yield done(id, False, e.detail)
        yield f"research failed: {e.detail}"
        return
    yield done(id, True, f"report from {found.sources} sources, added")
    yield {"t": "source", "src": Fetched.of(src).as_json()}
    yield f'added a research report on "{topic}" drawn from {found.sources} sources'


async def web_search(values: dict[str, str], t: Turn, query: str) -> list[research.Hit]:
    async with ledger.spending(t.owner, "web_search", collection_id=t.cid):
        return await research.web_search(values[st.SEARCH_MODEL_KEY], query)


async def state(t: Turn) -> Event:
    """The closing event: whether there is anything to build from, and the
    collection's title as it stands."""
    async with sessionmaker()() as s:
        title = await s.scalar(
            select(Collection.title).where(Collection.id == t.cid, Collection.owner_id == t.owner)
        )
    return {"t": "state", "ready": await source_count(t) > 0, "title": title or ""}


async def read_only(t: Turn) -> bool:
    async with sessionmaker()() as s:
        return bool(
            await s.scalar(
                select(Collection.read_only).where(
                    Collection.id == t.cid, Collection.owner_id == t.owner
                )
            )
        )


# ── the loop ─────────────────────────────────────────────────────────────────


async def run(t: Turn, history: Sequence[tuple[str, str]]) -> AsyncIterator[Event]:
    """One turn of the agent over the conversation so far, the person's
    newest message last. Every model call is charged to the person."""
    async with ledger.spending(t.owner, "agent", collection_id=t.cid):
        async for e in _run(t, history):
            yield e


async def _run(t: Turn, history: Sequence[tuple[str, str]]) -> AsyncIterator[Event]:
    last_user = next((text.strip() for role, text in reversed(history) if role == "user"), "")
    ids = Ids()
    read_this_turn: list[Fetched] = []

    # ── what they pasted ────────────────────────────────────────────────────
    # Links in the message are read before the model is asked anything, so
    # it answers knowing they are in. A long paste is kept as a note.
    urls, rest = split_material(last_user)
    urls = list(dict.fromkeys(urls))[: sources.MAX_URLS]
    if urls:
        started = [ids() for _ in urls]
        for id, u in zip(started, urls, strict=True):
            yield fetch_started(id, u)
        for id, f in zip(started, await fetch_urls(t, urls), strict=True):
            for e in fetched_events(id, f):
                yield e
            read_this_turn.append(f)
    if len(rest) >= NOTE_CHARS:
        id = ids()
        yield step(id, "note", "Keeping your note")
        f = await keep_note(t, rest)
        for e in fetched_events(id, f):
            yield e
        read_this_turn.append(f)

    # ── the agent ───────────────────────────────────────────────────────────
    ai = client.ai()
    if not ai.has_key:
        yield reply(NO_KEY)
        await _settle(t, read_this_turn)
        yield await state(t)
        return
    async with sessionmaker()() as s:
        values = await st.values(s, t.owner)
    model = values[st.CHAT_MODEL_KEY]
    language = st.language_rule(values[st.LANGUAGE_KEY])
    ro = await read_only(t)

    convo: list[dict[str, Any]] = [
        {"role": role, "content": text} for role, text in history[-HISTORY:] if text.strip()
    ]
    replied = False
    # Searched this turn / added a page this turn / already nudged once /
    # already corrected a reply that named a page it could not read.
    searched = added = nudged = corrected = build_nudged = False
    # Every search result this turn, in the order found, and every url
    # already tried. A page that cannot be read is replaced from here.
    pool: list[str] = []
    tried: set[str] = set()
    for _round in range(MAX_ROUNDS):
        yield {"t": "thinking"}
        names = await source_names(t)
        messages = [
            {
                "role": "system",
                "content": system_prompt(names, t.web, t.picks.output, language, ro),
            },
            *convo,
        ]
        try:
            resp = await ai.complete(model, messages, tools=tools(t.web), tool_choice="auto")
        except AiError as e:
            log.info("agent: the model call failed: %s", e)
            # Out of credit is not a hiccup, and "ask again" would fail the
            # same way: seen live as a 402 behind "I lost the model".
            yield reply(OUT_OF_CREDIT if e.kind == Kind.QUOTA else LOST_MODEL)
            replied = True
            break

        if not resp.tool_calls:
            # A search that found pages and a reply that lists them is the
            # failure this is for: the pages never reach the sources, so
            # nothing can be built. Once, the model is sent back to add them.
            if searched and not added and not nudged and not names and not ro:
                nudged = True
                convo.append({"role": "assistant", "content": resp.text})
                convo.append(
                    {
                        "role": "user",
                        "content": "(studio) Nothing has been added yet, so nothing can be "
                        "built. Call add_sources now with the best 2 to 4 URLs from your "
                        "search, then reply.",
                    }
                )
                continue
            # Asked to build once the sources were in, and the sources are
            # in, the model still stopped to ask "what would you like to do
            # next?". Once, it is reminded of what it was asked.
            if not build_nudged and not ro and asked_to_build(last_user) and names:
                build_nudged = True
                convo.append({"role": "assistant", "content": resp.text})
                convo.append(
                    {
                        "role": "user",
                        "content": "(studio) The person asked you to make something from the "
                        "sources once they were gathered, and there are sources now. Call "
                        "start_build now with the kind they asked for.",
                    }
                )
                continue
            # A reply that names a page it could NOT read claims a source
            # that is not there. Seen live: a search result on a dead host
            # was listed as "added" beside the three that were. Once, the
            # model is told which pages failed and asked to rewrite; after
            # that, any line naming one is dropped rather than shown.
            failed = [f.url for f in read_this_turn if not f.ok and f.url]
            # What the person would see: the reply without the model's
            # reasoning. Checked for failed pages after that is gone, since
            # reasoning about a failed page is not a claim that it was added.
            shown = without_reasoning(resp.text)
            if not corrected and any(u in shown for u in failed):
                corrected = True
                convo.append({"role": "assistant", "content": resp.text})
                convo.append(
                    {
                        "role": "user",
                        "content": "(studio) Your reply names pages that could NOT be read and "
                        f"are not sources: {', '.join(failed)}. Rewrite the reply naming only "
                        "pages that were added.",
                    }
                )
                continue
            text = without_failed(shown.strip(), failed)
            if text:
                yield reply(text)
                replied = True
            break

        convo.append(
            {
                "role": "assistant",
                "content": resp.text or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": _json(c.arguments)},
                    }
                    for c in resp.tool_calls
                ],
            }
        )
        building = answered = False
        last_made = ""
        for call in resp.tool_calls:
            args = call.arguments
            result: str
            if call.name == "web_search" and t.web:
                q = str(args.get("query") or "").strip()
                id = ids()
                yield step(id, "search", "Searching the web", q)
                searched = True
                try:
                    hits = await web_search(values, t, q)
                except AiError as e:
                    yield done(id, False, e.sentence)
                    result = f"search failed: {e.sentence}"
                else:
                    if hits:
                        yield done(id, True, f"{len(hits)} results")
                        pool.extend(h.url for h in hits if h.url not in pool)
                        result = _json(
                            [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in hits]
                        )
                    else:
                        yield done(id, False, "Nothing was found. Try other words.")
                        result = "search failed: no results"
            elif call.name == "add_sources":
                raw: Any = args.get("urls")
                asked = [
                    u
                    for u in (raw if isinstance(raw, list) else [])
                    if isinstance(u, str) and u.startswith(("http://", "https://"))
                ]
                asked = list(dict.fromkeys(asked))[:MAX_FETCH]
                if not asked:
                    result = "no http(s) urls given"
                else:
                    lines: list[str] = []
                    started = [ids() for _ in asked]
                    for id, u in zip(started, asked, strict=True):
                        yield fetch_started(id, u)
                    tried.update(asked)
                    need = 0
                    dead_hosts: set[str] = set()
                    got = await fetch_urls(t, asked)
                    for id, f in zip(started, got, strict=True):
                        for e in fetched_events(id, f):
                            yield e
                        if f.ok:
                            lines.append(f'added {f.url} — "{f.title}", about {f.chars // 6} words')
                        else:
                            need += 1
                            dead_hosts.add(research.short_host(f.url))
                            lines.append(f"could not read {f.url}: {f.error}")
                        added |= f.ok
                        read_this_turn.append(f)
                    # A page that cannot be read leaves the list and the next
                    # best search result takes its place, until the gap is
                    # filled or the results run out. Skips any host that
                    # already failed this call: a dead site tends to be dead
                    # for every page on it. Not in a read-only copy, where
                    # every page fails the same way.
                    attempts = 0
                    while need > 0 and attempts < MAX_REPLACE and not ro:
                        nxt = next_replacement(pool, tried, dead_hosts)
                        if nxt is None:
                            break
                        attempts += 1
                        tried.add(nxt)
                        id = ids()
                        yield step(
                            id, "fetch", "Trying another page instead", research.short_host(nxt)
                        )
                        (f,) = await fetch_urls(t, [nxt])
                        for e in fetched_events(id, f):
                            yield e
                        if f.ok:
                            need -= 1
                            added = True
                            lines.append(
                                f"replaced a page that failed with {f.url} — "
                                f'"{f.title}", about {f.chars // 6} words'
                            )
                        else:
                            dead_hosts.add(research.short_host(f.url))
                            lines.append(f"could not read {f.url} either: {f.error}")
                        read_this_turn.append(f)
                    result = "\n".join(lines)
            elif call.name == "deep_research" and t.web:
                topic = str(args.get("topic") or "").strip()
                result = "research failed"
                async for e in deep_research(t, ids, values, topic):
                    if isinstance(e, str):
                        result = e
                    else:
                        if e["t"] == "source":
                            read_this_turn.append(Fetched(url="", ok=True))
                        yield e
            elif call.name == "ask_sources":
                q = str(args.get("question") or "").strip()
                result = "asking the sources failed"
                async for e in ask(t, ids, values, q):
                    if isinstance(e, str):
                        result = e
                    else:
                        answered |= e["t"] == "reply"
                        yield e
            elif call.name == "start_build":
                if await source_count(t) == 0:
                    result = (
                        "refused: there are no sources yet. Find and add some first, or ask "
                        "the person for a link or notes."
                    )
                else:
                    title = str(args.get("title") or "").strip()
                    kind = wire_kind(str(args.get("kind") or "").strip(), t.picks.output)
                    made: Event | None = None
                    refused = ""
                    async for e in make(t, ids, kind, title, ""):
                        if e["t"] == "build":
                            made = e
                        elif e["t"] == "step_done" and not e["ok"]:
                            refused = e["text"]
                        yield e
                    if made is not None:
                        building = True
                        started = kind in ("session", "audio")
                        result = "it is being made" if started else "it is made"
                        last_made = kind
                    else:
                        result = f"refused: {refused} Tell the person in one sentence."
            else:
                result = f"unknown or unavailable tool `{call.name}`"
            convo.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if answered:
            replied = True
            break
        if building:
            yield reply(MAKING_IT if last_made in ("session", "audio") else MADE_IT)
            replied = True
            break
    if not replied:
        yield reply(FOUND_SO_FAR)
    await _settle(t, read_this_turn)
    yield await state(t)


async def _settle(t: Turn, read: list[Fetched]) -> None:
    """What was read this turn changed the collection's sources: name it and
    design its cover again, in the background, once for the whole turn."""
    if any(f.ok for f in read):
        refresh.spawn(t.owner, t.cid)


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)
