"""The Ask conversation of a collection, and the `/` commands.

The conversation is kept on the server, so every client sees the same one.
The commands are listed by the server and run by it: a client shows the menu
it is given and sends what was picked.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from opennotebook.api.deps import Db, Me, not_yet
from opennotebook.db.models import ChatMessage
from opennotebook.domain import collections

router = APIRouter(prefix="/api", tags=["chat"])


class Command(BaseModel):
    name: str = Field(description="Typed after the slash: /slides")
    arg: str = Field(description="What may follow it: [optional] or <required>; empty for none")
    label: str
    icon: str = Field(description="A bootstrap-icons name")


COMMANDS: list[Command] = [
    Command(name="slides", arg="[title]", label="Build narrated slides", icon="easel"),
    Command(name="audio", arg="[focus]", label="Make an audio overview", icon="soundwave"),
    Command(name="mindmap", arg="[focus]", label="Make a mind map", icon="diagram-3"),
    Command(name="notes", arg="[focus]", label="Make study notes", icon="journal-text"),
    Command(name="search", arg="<topic>", label="Find sources on the web", icon="search"),
    Command(name="research", arg="<topic>", label="Research a topic in depth", icon="stars"),
    Command(
        name="ask", arg="<question>", label="Ask your sources, with citations", icon="chat-dots"
    ),
    Command(name="help", arg="", label="What I can do", icon="info-circle"),
    Command(name="clear", arg="", label="Clear the conversation", icon="trash"),
]


class Message(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    text: str
    steps: list[dict[str, Any]] = Field(description="The work lines shown under an answer")
    citations: list[dict[str, Any]]
    created_at: datetime


class Say(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class RunCommand(BaseModel):
    name: str = Field(description="A command name from /api/commands")
    arg: str = Field(default="", max_length=2000)


@router.get("/commands")
async def list_commands(me: Me) -> list[Command]:
    """The `/` commands, in menu order."""
    return COMMANDS


@router.get("/collections/{cid}/chat")
async def read_chat(cid: uuid.UUID, s: Db, me: Me) -> list[Message]:
    """A collection's conversation, oldest first."""
    await collections.summary(s, me.id, cid)
    rows = await s.scalars(
        select(ChatMessage)
        .where(ChatMessage.collection_id == cid, ChatMessage.owner_id == me.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    return [Message.model_validate(m, from_attributes=True) for m in rows]


@router.delete("/collections/{cid}/chat", status_code=204)
async def clear_chat(cid: uuid.UUID, s: Db, me: Me) -> None:
    """Clear a collection's conversation."""
    await collections.summary(s, me.id, cid)
    await s.execute(
        delete(ChatMessage).where(ChatMessage.collection_id == cid, ChatMessage.owner_id == me.id)
    )


@router.post("/collections/{cid}/chat")
async def say(cid: uuid.UUID, body: Say, s: Db, me: Me) -> None:
    """Send a message. The answer streams back as server-sent events: thinking,
    step, step_note, step_done, source, reply, state. The turn is kept."""
    await collections.summary(s, me.id, cid)
    raise not_yet()


@router.post("/collections/{cid}/chat/commands")
async def run_command(cid: uuid.UUID, body: RunCommand, s: Db, me: Me) -> None:
    """Run a `/` command. It streams the same events a message does."""
    await collections.summary(s, me.id, cid)
    raise not_yet()
