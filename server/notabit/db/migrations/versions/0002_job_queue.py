"""The job queue's own tables and functions, as Procrastinate ships them.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op
from procrastinate.schema import SchemaManager

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Straight to psycopg, with no parameters: the script is many statements,
    # and its RAISE formats use `%`, which must not be read as placeholders.
    psycopg_conn = op.get_bind().connection.driver_connection
    assert psycopg_conn is not None
    psycopg_conn.execute(SchemaManager.get_schema())


def downgrade() -> None:
    # Everything Procrastinate made is named procrastinate_*: tables, their
    # functions and their types.
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT tablename FROM pg_tables
                   WHERE schemaname = 'public' AND tablename LIKE 'procrastinate\\_%' LOOP
            EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', r.tablename);
          END LOOP;
          FOR r IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
                   JOIN pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%' LOOP
            EXECUTE 'DROP FUNCTION IF EXISTS ' || r.fn || ' CASCADE';
          END LOOP;
          FOR r IN SELECT t.typname FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE n.nspname = 'public' AND t.typname LIKE 'procrastinate\\_%'
                     AND t.typtype IN ('e', 'c') LOOP
            EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', r.typname);
          END LOOP;
        END $$;
        """
    )
