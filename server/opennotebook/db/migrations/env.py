"""Alembic's entry point. Migrations run on a synchronous psycopg connection
to the database in DATABASE_URL, or to the URL a caller put in the config
(the tests point it at TEST_DATABASE_URL)."""

from alembic import context
from sqlalchemy import create_engine, pool

from opennotebook.config import settings
from opennotebook.db.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or settings().sqlalchemy_url


def ours(name: str | None, type_: str, _parent: object) -> bool:
    """Leave the job queue's tables to Procrastinate, which ships its own."""
    return not (type_ == "table" and (name or "").startswith("procrastinate_"))


connectable = create_engine(url, poolclass=pool.NullPool)
with connectable.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_name=ours,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()
