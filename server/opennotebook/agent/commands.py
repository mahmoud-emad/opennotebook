"""The `/` commands of the Ask chat, run by the server. A port of the
command half of `opennotebook_ui/src/chat.rs` and of `send_chat` in
`collection.rs`, which ran them in the browser.

Every client sees the same commands and gets the same answers: the menu is
`COMMANDS`, `/help` is written from it, and a command that makes something
starts it here rather than asking the page to. The makers come first in the
menu: they are what the person came for.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from opennotebook.agent import loop
from opennotebook.agent.loop import Event, Ids, Turn
from opennotebook.db.session import sessionmaker
from opennotebook.domain import settings as st


@dataclass(frozen=True)
class Command:
    """One entry of the `/` menu."""

    name: str
    # What follows the name, as the menu shows it; empty for none.
    arg: str
    label: str
    # A bootstrap-icons name.
    icon: str


COMMANDS: list[Command] = [
    Command("slides", "[title]", "Build narrated slides", "easel"),
    Command("audio", "[focus]", "Make an audio overview", "soundwave"),
    Command("mindmap", "[focus]", "Make a mind map", "diagram-3"),
    Command("notes", "[focus]", "Make study notes", "journal-text"),
    Command("search", "<topic>", "Find sources on the web", "search"),
    Command("research", "<topic>", "Research a topic in depth", "stars"),
    Command("ask", "<question>", "Ask your sources, with citations", "chat-dots"),
    Command("help", "", "What I can do", "info-circle"),
    Command("clear", "", "Clear the conversation", "trash"),
]

# The makers, by command name, as the wire names the kind.
MAKERS = {"slides": "session", "audio": "audio", "mindmap": "mindmap", "notes": "notes"}


def find(name: str) -> Command | None:
    name = name.lower()
    return next((c for c in COMMANDS if c.name == name), None)


def parse_command(text: str) -> tuple[str, str] | None:
    """A message as a command: None when it is not one, else the name
    (lowercased, whether or not a command has it) and what followed it."""
    t = text.strip()
    if not t.startswith("/"):
        return None
    # Any whitespace ends the name, not only a space.
    parts = t[1:].split(maxsplit=1)
    name = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    return name.lower(), arg.strip()


def help_text() -> str:
    """The `/help` answer: everything the studio does, from the same list the
    menu shows."""
    s = (
        "**What I can do**\n\n"
        "Everything I make comes from the sources on the left. Add your own links and files "
        "there, paste a link here, or let me find pages.\n\n"
    )
    for c in COMMANDS:
        s += f"- `/{c.name}{' ' + c.arg if c.arg else ''}` — {c.label}\n"
    return (
        s + "\nOr just tell me what you want to learn, and say build when you are happy with "
        "the sources."
    )


def unknown(name: str) -> str:
    return f"There is no /{name} command. Type / to see the ones there are."


async def run(
    t: Turn, name: str, arg: str, history: Sequence[tuple[str, str]]
) -> AsyncIterator[Event]:
    """Run one command, as the events a chat turn streams. `/clear` is the
    route's, since it is not a turn. `/search` and `/research` go to the
    agent as typed, and its prompt reads them."""
    c = find(name)
    arg = arg.strip()
    if c is None:
        yield loop.reply(unknown(name))
        return
    if c.name == "help":
        yield loop.reply(help_text())
        return
    if c.name in ("search", "research"):
        if not arg:
            yield loop.reply(f"What should I {c.name}? Type the topic after /{c.name}.")
            return
        # A command is a request for the web, whatever the page's switch.
        t.web = True
        async for e in loop.run(t, history):
            yield e
        return
    if c.name == "ask":
        if not arg:
            yield loop.reply("Ask what? Type the question after /ask.")
            return
        async with sessionmaker()() as s:
            values = await st.values(s, t.owner)
        async for e in loop.ask(t, Ids(), values, arg):
            if not isinstance(e, str):
                yield e
        return
    kind = MAKERS.get(c.name)
    if kind is None:
        yield loop.reply(unknown(name))
        return
    async for e in _make(t, kind, arg):
        yield e


async def _make(t: Turn, kind: str, arg: str) -> AsyncIterator[Event]:
    """Make one of the four outputs from the sources, at once. What follows
    the name is a deck's title, or what the others should focus on."""
    label = loop.LABELS[kind]
    n = await loop.source_count(t)
    if n == 0:
        yield loop.reply(
            f"I make {label} from your sources, and there are none yet. Add a link or a file "
            "on the left, or try `/search` and a topic."
        )
        return
    title, focus = (arg, "") if kind == "session" else ("", arg)
    made = False
    why = ""
    async for e in loop.make(t, Ids(), kind, title, focus):
        if e["t"] == "build":
            made = True
        elif e["t"] == "step_done" and not e["ok"]:
            why = e["text"]
        yield e
    if not made:
        yield loop.reply(f"I could not make {label}. {why}".strip())
        return
    sources = "your source" if n == 1 else f"your {n} sources"
    where = (
        "It appears in the Studio tab and takes a few minutes."
        if kind in ("session", "audio")
        else "It opens beside the chat, and it is in the Studio tab."
    )
    verb = "Making" if kind in ("session", "audio") else "Made"
    yield loop.reply(f"{verb} {label} from {sources}. {where}")
