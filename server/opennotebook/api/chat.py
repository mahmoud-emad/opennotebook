"""The Ask conversation of a collection, and the `/` commands.

The conversation is kept on the server, so every client sees the same one.
The commands are listed by the server and run by it: a client shows the menu
it is given and sends what was picked. A turn streams as server-sent events,
one JSON object a frame (see `opennotebook.agent.loop` for the vocabulary),
and both what was said and what was done are kept when it ends.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.agent import commands, loop
from opennotebook.agent.loop import Event, Picks, Turn
from opennotebook.api.deps import Db, Me
from opennotebook.auth import current_user
from opennotebook.db.models import ChatMessage, Collection, User
from opennotebook.db.session import sessionmaker
from opennotebook.errors import SERVER_FAULT, Problem, not_found
from opennotebook.shutdown import finish

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class Command(BaseModel):
    name: str = Field(description="Typed after the slash: /slides")
    arg: str = Field(description="What may follow it: [optional] or <required>; empty for none")
    label: str
    icon: str = Field(description="A bootstrap-icons name")


class Message(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    text: str
    steps: list[dict[str, Any]] = Field(
        description="The work lines shown under an answer: id, kind, text, detail, note and "
        "status (ok, bad)"
    )
    citations: list[dict[str, Any]]
    created_at: datetime


class PagePicks(BaseModel):
    """What the page has picked, used when the person does not say."""

    output: Literal["", "session", "audio", "mindmap", "notes"] = Field(
        default="",
        description="What a build makes when the person names no kind; empty is slides",
    )
    style: str | None = Field(default=None, description="A deck's style, from /api/styles")
    audio_format: Literal["deep_dive", "brief", "critique", "debate"] | None = None
    audio_length: Literal["shorter", "default", "longer"] | None = None

    def picks(self) -> Picks:
        return Picks(self.output, self.style, self.audio_format, self.audio_length)


class Say(PagePicks):
    text: str = Field(min_length=1, max_length=8000)
    research: bool = Field(
        default=True,
        description="Whether the agent may search and research the web; off, it reads only "
        "the links it is given",
    )


class RunCommand(PagePicks):
    name: str = Field(description="A command name from /api/commands")
    arg: str = Field(default="", max_length=2000)
    text: str = Field(
        default="",
        max_length=8000,
        description="What the conversation keeps as said; /name arg when empty",
    )


SSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}
}


@router.get("/commands")
async def list_commands(me: Me) -> list[Command]:
    """The `/` commands, in menu order."""
    return [Command(name=c.name, arg=c.arg, label=c.label, icon=c.icon) for c in commands.COMMANDS]


@router.get("/collections/{cid}/chat")
async def read_chat(cid: uuid.UUID, s: Db, me: Me) -> list[Message]:
    """A collection's conversation, oldest first."""
    await _mine(s, me.id, cid)
    rows = await s.scalars(
        select(ChatMessage)
        .where(ChatMessage.collection_id == cid, ChatMessage.owner_id == me.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    return [Message.model_validate(m, from_attributes=True) for m in rows]


@router.delete("/collections/{cid}/chat", status_code=204)
async def clear_chat(cid: uuid.UUID, s: Db, me: Me) -> None:
    """Clear a collection's conversation."""
    await _mine(s, me.id, cid)
    await _clear(s, me.id, cid)


@router.post("/collections/{cid}/chat", response_class=StreamingResponse, responses=SSE)
async def say(cid: uuid.UUID, body: Say, request: Request) -> StreamingResponse:
    """Send a message. The agent answers as server-sent events: thinking,
    step, step_note, step_done, source, reply, build, state. It may read
    pages into the sources, research, answer from the sources with
    citations, and start a build; a read-only copy can be asked, and
    refuses the rest. The turn is kept."""
    text = body.text.strip()
    if not text:
        raise Problem(422, "The message is empty. Write something, then send it.")
    me, history = await _begin(request, cid, text)
    turn = Turn(me=me, cid=cid, picks=body.picks(), web=body.research)
    return _stream(turn, loop.run(turn, history))


@router.post("/collections/{cid}/chat/commands", response_class=StreamingResponse, responses=SSE)
async def run_command(cid: uuid.UUID, body: RunCommand, request: Request) -> StreamingResponse:
    """Run a `/` command. It streams the same events a message does, and is
    kept like one. `/clear` clears the conversation and streams `cleared`."""
    name = body.name.strip().lstrip("/").lower()
    arg = body.arg.strip()
    if name == "clear":
        async with sessionmaker()() as s, s.begin():
            me = await current_user(request, s)
            await _mine(s, me.id, cid)
            await _clear(s, me.id, cid)
        return _respond(_once({"t": "cleared"}))
    said = body.text.strip() or f"/{name}{' ' + arg if arg else ''}"
    me, history = await _begin(request, cid, said)
    turn = Turn(me=me, cid=cid, picks=body.picks())
    return _stream(turn, commands.run(turn, name, arg, history))


# ── the conversation ─────────────────────────────────────────────────────────


async def _mine(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> None:
    found = await s.scalar(
        select(Collection.id).where(Collection.id == cid, Collection.owner_id == owner)
    )
    if found is None:
        raise not_found("That collection")


async def _clear(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> None:
    await s.execute(
        delete(ChatMessage).where(ChatMessage.collection_id == cid, ChatMessage.owner_id == owner)
    )


async def _begin(request: Request, cid: uuid.UUID, said: str) -> tuple[User, list[tuple[str, str]]]:
    """Who is asking, with what they said kept, and the conversation so far
    for the model: the newest turns, oldest first, this one last.

    Its own session, committed before the stream starts: a turn lasts up to
    minutes, and holds no transaction while it does."""
    async with sessionmaker()() as s, s.begin():
        me = await current_user(request, s)
        await _mine(s, me.id, cid)
        s.add(ChatMessage(owner_id=me.id, collection_id=cid, role="user", text=said))
        await s.flush()
        rows = list(
            await s.execute(
                select(ChatMessage.role, ChatMessage.text)
                .where(ChatMessage.collection_id == cid, ChatMessage.owner_id == me.id)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(loop.HISTORY)
            )
        )
    return me, [(r.role, r.text) for r in reversed(rows)]


class Record:
    """What a turn did, as it is kept: the work lines with their results,
    what was said, and the citations of an answer from the sources."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.replies: list[str] = []
        self.citations: list[dict[str, Any]] = []

    def see(self, e: Event) -> None:
        t = e.get("t")
        if t == "step":
            self.steps.append(
                {
                    "id": e["id"],
                    "kind": e["kind"],
                    "text": e["text"],
                    "detail": e["detail"],
                    "note": "",
                    "status": "run",
                }
            )
        elif t in ("step_note", "step_done"):
            for st in reversed(self.steps):
                if st["id"] == e["id"]:
                    st["note"] = e["text"]
                    if t == "step_done":
                        st["status"] = "ok" if e["ok"] else "bad"
                    break
        elif t == "reply":
            self.replies.append(e["text"])
            if e.get("citations"):
                self.citations = e["citations"]

    def message(self, owner: uuid.UUID, cid: uuid.UUID) -> ChatMessage | None:
        if not self.steps and not self.replies:
            return None
        for st in self.steps:
            # A line still running when the turn ended never finished.
            if st["status"] == "run":
                st["status"] = "bad"
                st["note"] = st["note"] or "Stopped before it finished."
        return ChatMessage(
            owner_id=owner,
            collection_id=cid,
            role="assistant",
            text="\n\n".join(self.replies),
            steps=self.steps,
            citations=self.citations,
        )


# Turns still running, held so none is collected mid-turn, and so the api's
# shutdown can let them finish (`drain`).
_turns: set[asyncio.Task[None]] = set()


async def drain(seconds: float) -> None:
    """At shutdown: give the running turns `seconds` to finish, then stop
    the rest. A stopped turn still keeps what it said and did."""
    await finish(_turns, seconds)


def _stream(turn: Turn, events: AsyncIterator[Event]) -> StreamingResponse:
    """Run a turn to its end and keep it, whether or not anyone is still
    reading: a closed tab stops the stream, not the work, so what was
    started is finished and what was said is kept."""
    out: asyncio.Queue[Event | None] = asyncio.Queue()

    async def work() -> None:
        rec = Record()
        try:
            async for e in events:
                rec.see(e)
                out.put_nowait(e)
        except Exception:
            log.exception("chat turn in %s failed", turn.cid)
            e = loop.reply(SERVER_FAULT)
            rec.see(e)
            out.put_nowait(e)
        finally:
            try:
                await asyncio.shield(_keep(rec, turn))
            finally:
                out.put_nowait(None)

    task = asyncio.get_running_loop().create_task(work())
    _turns.add(task)
    task.add_done_callback(_turns.discard)

    async def frames() -> AsyncIterator[Event]:
        while (e := await out.get()) is not None:
            yield e

    return _respond(frames())


async def _keep(rec: Record, turn: Turn) -> None:
    msg = rec.message(turn.owner, turn.cid)
    if msg is None:
        return
    try:
        async with sessionmaker()() as s, s.begin():
            # Not into a collection deleted while the turn ran.
            await _mine(s, turn.owner, turn.cid)
            s.add(msg)
    except Problem:
        return


async def _once(*events: Event) -> AsyncIterator[Event]:
    for e in events:
        yield e


def _respond(events: AsyncIterator[Event]) -> StreamingResponse:
    async def frames() -> AsyncIterator[str]:
        async for e in events:
            yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
