"""The whole schema: people, settings, collections and what they hold, search
and background jobs. The studio starts empty, so this is all of it.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "instance_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_instance_settings")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), server_default="", nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_api_keys_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("hash", name=op.f("uq_api_keys_hash")),
    )
    op.create_index(op.f("ix_api_keys_owner_id"), "api_keys", ["owner_id"], unique=False)
    op.create_table(
        "collections",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("title_auto", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cover", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cover_version", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_collections_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
    )
    op.create_index(
        "ix_collections_owner_updated", "collections", ["owner_id", "updated_at"], unique=False
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("speech_chars", sa.Integer(), server_default="0", nullable=False),
        sa.Column("speech_seconds", sa.Float(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("priced_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "priced_by IN ('provider', 'catalogue', 'unpriced')",
            name=op.f("ck_usage_events_priced_by"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_usage_events_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
    )
    op.create_index(
        "ix_usage_events_owner_created", "usage_events", ["owner_id", "created_at"], unique=False
    )
    op.create_table(
        "user_settings",
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_user_settings_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_id", "key", name=op.f("pk_user_settings")),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "steps", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_chat_messages_role")),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_chat_messages_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_chat_messages_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    op.create_index(
        "ix_chat_messages_collection_created",
        "chat_messages",
        ["collection_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_chat_messages_owner_id"), "chat_messages", ["owner_id"], unique=False)
    op.create_table(
        "mindmaps",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("focus", sa.Text(), server_default="", nullable=False),
        sa.Column("sources", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("excerpted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("node_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dropped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchecked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("shape", sa.ARRAY(sa.Integer()), server_default="{}", nullable=False),
        sa.Column("root", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_mindmaps_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_mindmaps_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mindmaps")),
    )
    op.create_index(
        "ix_mindmaps_collection_created", "mindmaps", ["collection_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_mindmaps_owner_id"), "mindmaps", ["owner_id"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("state", sa.Text(), server_default="preparing", nullable=False),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("style", sa.Text(), nullable=True),
        sa.Column(
            "speakers", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "slides", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("audio", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("deck_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("spent_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("spent_known", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('slides', 'audio')", name=op.f("ck_sessions_kind")),
        sa.CheckConstraint(
            "state IN ('preparing', 'ready', 'failed')", name=op.f("ck_sessions_state")
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_sessions_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_sessions_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index(
        "ix_sessions_collection_created", "sessions", ["collection_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_sessions_owner_id"), "sessions", ["owner_id"], unique=False)
    op.create_table(
        "sources",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("url", sa.Text(), server_default="", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("chars", sa.Integer(), server_default="0", nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('url', 'text', 'file', 'research')", name=op.f("ck_sources_kind")
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_sources_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_sources_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("collection_id", "name", name=op.f("uq_sources_collection_id")),
    )
    op.create_index(op.f("ix_sources_collection_id"), "sources", ["collection_id"], unique=False)
    op.create_index(op.f("ix_sources_owner_id"), "sources", ["owner_id"], unique=False)
    op.create_table(
        "study_notes",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("focus", sa.Text(), server_default="", nullable=False),
        sa.Column("sources", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("excerpted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("dropped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchecked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_study_notes_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_study_notes_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_notes")),
    )
    op.create_index(
        "ix_study_notes_collection_created",
        "study_notes",
        ["collection_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_study_notes_owner_id"), "study_notes", ["owner_id"], unique=False)
    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(), nullable=True),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_chunks_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_chunks_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_chunks_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
    )
    op.create_index(
        "ix_chunks_collection_source_ord",
        "chunks",
        ["collection_id", "source_id", "ord"],
        unique=False,
    )
    op.create_index(op.f("ix_chunks_owner_id"), "chunks", ["owner_id"], unique=False)
    op.create_index(op.f("ix_chunks_source_id"), "chunks", ["source_id"], unique=False)
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], unique=False, postgresql_using="gin")
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("step", sa.Text(), server_default="", nullable=False),
        sa.Column("steps_done", sa.Integer(), server_default="0", nullable=False),
        sa.Column("steps_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("procrastinate_job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_jobs_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_jobs_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_jobs_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_collection_id"), "jobs", ["collection_id"], unique=False)
    op.create_index(op.f("ix_jobs_owner_id"), "jobs", ["owner_id"], unique=False)
    op.create_index(op.f("ix_jobs_session_id"), "jobs", ["session_id"], unique=False)
    op.create_table(
        "playback",
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("slide_ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("line_id", sa.Text(), server_default="", nullable=False),
        sa.Column("offset_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("state", sa.Text(), server_default="idle", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('idle', 'playing', 'paused', 'finished')", name=op.f("ck_playback_state")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_playback_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_playback_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_playback")),
    )
    op.create_index(op.f("ix_playback_owner_id"), "playback", ["owner_id"], unique=False)
    op.create_table(
        "qa_pairs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', question || ' ' || answer)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(), nullable=True),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_qa_pairs_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_qa_pairs_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_qa_pairs_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_pairs")),
    )
    op.create_index(op.f("ix_qa_pairs_collection_id"), "qa_pairs", ["collection_id"], unique=False)
    op.create_index(op.f("ix_qa_pairs_owner_id"), "qa_pairs", ["owner_id"], unique=False)
    op.create_index(op.f("ix_qa_pairs_source_id"), "qa_pairs", ["source_id"], unique=False)
    op.create_index("ix_qa_pairs_tsv", "qa_pairs", ["tsv"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_qa_pairs_tsv", table_name="qa_pairs", postgresql_using="gin")
    op.drop_index(op.f("ix_qa_pairs_source_id"), table_name="qa_pairs")
    op.drop_index(op.f("ix_qa_pairs_owner_id"), table_name="qa_pairs")
    op.drop_index(op.f("ix_qa_pairs_collection_id"), table_name="qa_pairs")
    op.drop_table("qa_pairs")
    op.drop_index(op.f("ix_playback_owner_id"), table_name="playback")
    op.drop_table("playback")
    op.drop_index(op.f("ix_jobs_session_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_owner_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_collection_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_chunks_tsv", table_name="chunks", postgresql_using="gin")
    op.drop_index(op.f("ix_chunks_source_id"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_owner_id"), table_name="chunks")
    op.drop_index("ix_chunks_collection_source_ord", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index(op.f("ix_study_notes_owner_id"), table_name="study_notes")
    op.drop_index("ix_study_notes_collection_created", table_name="study_notes")
    op.drop_table("study_notes")
    op.drop_index(op.f("ix_sources_owner_id"), table_name="sources")
    op.drop_index(op.f("ix_sources_collection_id"), table_name="sources")
    op.drop_table("sources")
    op.drop_index(op.f("ix_sessions_owner_id"), table_name="sessions")
    op.drop_index("ix_sessions_collection_created", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_mindmaps_owner_id"), table_name="mindmaps")
    op.drop_index("ix_mindmaps_collection_created", table_name="mindmaps")
    op.drop_table("mindmaps")
    op.drop_index(op.f("ix_chat_messages_owner_id"), table_name="chat_messages")
    op.drop_index("ix_chat_messages_collection_created", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_table("user_settings")
    op.drop_index("ix_usage_events_owner_created", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("ix_collections_owner_updated", table_name="collections")
    op.drop_table("collections")
    op.drop_index(op.f("ix_api_keys_owner_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("instance_settings")
