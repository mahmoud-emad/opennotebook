"""The tables, as SQLAlchemy models.

Every table that holds a person's things carries `owner_id`, and every query
filters on it: the repository functions take the owner as their first
argument, and `tests/test_owners.py` proves one person cannot read another's
records. Ids are UUIDv7 made by Postgres 18 (`uuidv7()`), so they sort by
creation time.

Alembic owns the schema. A change here is a migration in `db/migrations`.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Constraint names Alembic can rely on, so a later migration can drop one.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))


def _owner() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)


def _collection() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=text("now()"))


def _flag(default: bool = False) -> Mapped[bool]:
    return mapped_column(Boolean, server_default=text("true" if default else "false"))


def _str() -> Mapped[str]:
    return mapped_column(Text, server_default="")


def _count() -> Mapped[int]:
    return mapped_column(Integer, server_default="0")


# ── People ────────────────────────────────────────────────────────────────────


class User(Base):
    """A person, or the one owner of a studio run locally.

    How people sign in is not decided (docs/open-questions.md); nothing here
    depends on the answer.
    """

    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = _str()
    created_at: Mapped[datetime] = _now()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    """A key an agent or a script uses to act as its owner.

    Only a hash is stored. The key is shown once, when it is made; `prefix` is
    its first characters, so a person can tell their keys apart.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    name: Mapped[str] = _str()
    prefix: Mapped[str] = mapped_column(String(16))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageEvent(Base):
    """One paid call: a model, speech or search call, and what it cost.

    The ledger the credit calculator will read. Nothing about plans or credits
    is decided, so nothing about them is stored; this is only what was used.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("priced_by IN ('provider', 'catalogue', 'unpriced')", name="priced_by"),
        Index("ix_usage_events_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # What it was for: chat, ask, script, slides, narration, research, …
    kind: Mapped[str] = mapped_column(Text)
    # Plain columns, not foreign keys: the ledger outlives what it paid for.
    collection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str] = _str()
    input_tokens: Mapped[int] = _count()
    output_tokens: Mapped[int] = _count()
    speech_chars: Mapped[int] = _count()
    speech_seconds: Mapped[float] = mapped_column(Float, server_default="0")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    priced_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


# ── Settings ──────────────────────────────────────────────────────────────────


class InstanceSetting(Base):
    """What whoever runs the studio sets: endpoints, the model catalogue,
    defaults. Secrets stay in the environment, never here."""

    __tablename__ = "instance_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _now()


class UserSetting(Base):
    """What a person chooses: style, voices, language, their spending limit.
    A key with no row here takes the instance value, then the default."""

    __tablename__ = "user_settings"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _now()


# ── Collections and what they hold ────────────────────────────────────────────


class Collection(Base):
    """A set of sources, and everything made from them.

    Writes to a collection and what it holds take `SELECT … FOR UPDATE` on
    this row first. That is the lock the Rust server kept in memory, and it
    works across the api and the worker.
    """

    __tablename__ = "collections"
    __table_args__ = (Index("ix_collections_owner_updated", "owner_id", "updated_at"),)

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    title: Mapped[str] = _str()
    # The studio named it from its sources; a person's title is never replaced.
    title_auto: Mapped[bool] = _flag(True)
    # The source names the studio last named it from, one per line, so a
    # run that finds the same sources asks no model.
    titled_from: Mapped[str] = _str()
    pinned: Mapped[bool] = _flag()
    # The designed cover's spec (topic, terms, motif, palette, layout); none
    # until one is designed, and the cover is drawn from the cid and title.
    cover: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The designed cover's version, written with it.
    cover_version: Mapped[str] = _str()
    # What the cover was last designed from (`domain.covers.content_key`),
    # tried or done, so a model that is down is asked again only when the
    # content changes.
    cover_from: Mapped[str] = _str()
    # The share this collection was copied from by a reuse; none when it was
    # not. A plain column, not a foreign key: the share may since have been
    # removed, and the copy is the reuser's whatever happens to it.
    reused_from: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # A copy whose share did not allow edits: it is read, asked and played,
    # but nothing in it is changed and it is not shared again. Set when it is
    # made, and never changed after, whatever happens to the share.
    read_only: Mapped[bool] = _flag()
    created_at: Mapped[datetime] = _now()
    # The last time a source or an output was added or changed.
    updated_at: Mapped[datetime] = _now()


class Source(Base):
    """One source, its text verbatim as Markdown. The original upload, when
    there was one, is on the files volume at `file_path`."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("collection_id", "name"),
        CheckConstraint("kind IN ('url', 'text', 'file', 'research')", name="kind"),
        Index("ix_sources_collection_created", "collection_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = _collection()
    # The stable name clients address it by, unique in its collection.
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = _str()
    url: Mapped[str] = _str()
    text: Mapped[str] = mapped_column(Text)
    chars: Mapped[int] = _count()
    file_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Session(Base):
    """An output that is built in the background: a narrated slide deck or an
    audio overview. `slides` holds the parts with their narration lines, read
    and written whole."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("kind IN ('slides', 'audio')", name="kind"),
        CheckConstraint("state IN ('preparing', 'ready', 'failed')", name="state"),
        Index("ix_sessions_collection_created", "collection_id", "created_at"),
        Index("ix_sessions_owner_created", "owner_id", "created_at"),
        # Only the outputs still being made: what a collection's busy count
        # and its progress read.
        Index(
            "ix_sessions_preparing",
            "collection_id",
            postgresql_where=text("state = 'preparing'"),
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = _str()
    description: Mapped[str] = _str()
    state: Mapped[str] = mapped_column(Text, server_default="preparing")
    # Why a failed output failed, in a sentence a person can act on.
    failure: Mapped[str | None] = mapped_column(Text)
    style: Mapped[str | None] = mapped_column(Text)
    speakers: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    slides: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    # Format, length, focus and minutes, for an audio overview.
    audio: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deck_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    duration_ms: Mapped[int] = mapped_column(BigInteger, server_default="0")
    spent_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # False when any call of the build could not be priced.
    spent_known: Mapped[bool] = _flag()
    pinned: Mapped[bool] = _flag()
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Playback(Base):
    """Where a session's playback is. Kept in memory before, and lost on a
    restart; a row now."""

    __tablename__ = "playback"
    __table_args__ = (
        CheckConstraint("state IN ('idle', 'playing', 'paused', 'finished')", name="state"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[uuid.UUID] = _owner()
    slide_ordinal: Mapped[int] = _count()
    line_id: Mapped[str] = _str()
    offset_ms: Mapped[int] = mapped_column(BigInteger, server_default="0")
    state: Mapped[str] = mapped_column(Text, server_default="idle")
    updated_at: Mapped[datetime] = _now()


class MindMap(Base):
    __tablename__ = "mindmaps"
    __table_args__ = (
        Index("ix_mindmaps_collection_created", "collection_id", "created_at"),
        CheckConstraint("state IN ('making', 'ready')", name="state"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    title: Mapped[str] = _str()
    focus: Mapped[str] = _str()
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    excerpted: Mapped[bool] = _flag()
    model: Mapped[str] = _str()
    node_count: Mapped[int] = _count()
    # Nodes removed because the sources never mention them.
    dropped: Mapped[int] = _count()
    # The check against the sources did not run: the map is not in English.
    unchecked: Mapped[bool] = _flag()
    # How many subtopics each main topic has: the outline, for a cover.
    shape: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default="{}")
    root: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # `making` while its job writes it, then `ready`. One whose making fails
    # is removed; its job says why.
    state: Mapped[str] = mapped_column(Text, server_default="ready")
    # The job that makes it. A plain column, not a foreign key: old jobs are
    # pruned, and the map stays.
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()


class StudyNotes(Base):
    __tablename__ = "study_notes"
    __table_args__ = (
        Index("ix_study_notes_collection_created", "collection_id", "created_at"),
        CheckConstraint("state IN ('making', 'ready')", name="state"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    title: Mapped[str] = _str()
    focus: Mapped[str] = _str()
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    excerpted: Mapped[bool] = _flag()
    model: Mapped[str] = _str()
    # Citations removed: a number naming no passage, or a passage that does
    # not say the claim.
    dropped: Mapped[int] = _count()
    unchecked: Mapped[bool] = _flag()
    # Overview, ideas, quiz, essays, glossary, citations and the Markdown
    # export, read and written whole.
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # As a map's: `making` while its job writes it, then `ready`.
    state: Mapped[str] = mapped_column(Text, server_default="ready")
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()


class ChatMessage(Base):
    """One turn of a collection's Ask conversation. Kept in the browser
    before; on the server now, so every client sees the same history."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        Index("ix_chat_messages_collection_created", "collection_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    # The work lines shown under an answer: searching, reading, building.
    steps: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    citations: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = _now()


# ── Sharing ───────────────────────────────────────────────────────────────────


class Share(Base):
    """A collection put in the studio's public feed, Discover. At most one per
    collection, so unsharing and deleting the collection need no lookup; the
    collection's delete takes its share with it.

    A share is a live view, not a frozen copy: the title, the cover and the
    sources are the collection's as they are now. `outputs` are keys,
    `session:<id>`, `mindmap:<id>` or `notes:<id>`, in the order the owner
    picked them. An output deleted after it was shared is not taken off here;
    every reader skips a key that no longer resolves.
    """

    __tablename__ = "shares"
    __table_args__ = (
        UniqueConstraint("collection_id"),
        Index("ix_shares_updated", "updated_at"),
        # Discover's "most reused" order.
        Index("ix_shares_reuses", "reuses", "updated_at", "id"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    include_sources: Mapped[bool] = _flag()
    outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # The owner's note, at most 280 characters; empty when none.
    note: Mapped[str] = _str()
    # How many times it was reused.
    reuses: Mapped[int] = _count()
    # Copies made from it are their reuser's to change and share again; when
    # not, each copy is read-only. A change applies to copies made after it.
    allow_edits: Mapped[bool] = _flag()
    created_at: Mapped[datetime] = _now()
    # The last time the share was set: the feed's "newest" order.
    updated_at: Mapped[datetime] = _now()


# ── Search ────────────────────────────────────────────────────────────────────
#
# Keywords through a generated tsvector with GIN, vectors through pgvector.
# `embedding` has no fixed dimension: embeddings are optional and the model is
# a setting (OPENNOTEBOOK_EMBED_MODEL). The HNSW index needs a dimension, so it
# is made by a migration once a model is chosen, as an expression index on
# `embedding::vector(n)`.


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_collection_source_ord", "collection_id", "source_id", "ord"),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    ord: Mapped[int] = mapped_column(Integer)
    # Where the passage sits in its source's text, as Python string offsets:
    # `source.text[start:end] == text`.
    start: Mapped[int] = _count()
    end: Mapped[int] = _count()
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    # Vectors from different models are never compared: each carries its model.
    embed_model: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any | None] = mapped_column(Vector())


class QaPair(Base):
    __tablename__ = "qa_pairs"
    __table_args__ = (Index("ix_qa_pairs_tsv", "tsv", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID] = _collection()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    dimension: Mapped[str] = _str()
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', question || ' ' || answer)", persisted=True),
    )
    embed_model: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any | None] = mapped_column(Vector())


# ── Background work ───────────────────────────────────────────────────────────


class Job(Base):
    """A piece of background work and how far it got. The queue itself is
    Procrastinate's; this row is what clients read for progress, and what
    `NOTIFY job_progress` points at."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'cancelled')", name="status"
        ),
        # An output's newest job.
        Index("ix_jobs_session_created", "session_id", "created_at"),
        # The row of a queue job, when the worker recovers or fails it.
        Index(
            "ix_jobs_procrastinate_job_id",
            "procrastinate_job_id",
            postgresql_where=text("procrastinate_job_id IS NOT NULL"),
        ),
        # Finished rows, for the prune that removes old ones.
        Index("ix_jobs_finished", "finished_at", postgresql_where=text("finished_at IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = _id()
    owner_id: Mapped[uuid.UUID] = _owner()
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    step: Mapped[str] = _str()
    steps_done: Mapped[int] = _count()
    steps_total: Mapped[int] = _count()
    error: Mapped[str | None] = mapped_column(Text)
    procrastinate_job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
