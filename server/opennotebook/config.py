"""Settings read from the environment, once.

Only what an operator sets lives here: where the database and the files are,
which AI endpoint to call and with what key, how people sign in. What a person
chooses (style, voices, language) is a user setting in the database.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENNOTEBOOK_", extra="ignore")

    # `postgresql+psycopg://…`; a plain `postgresql://` URL is accepted too.
    database_url: str = Field(validation_alias="DATABASE_URL")
    files_dir: Path = Path("./data/files")

    ai_base_url: str = "https://openrouter.ai/api/v1"
    # The names the Rust studio read still work: OPENNOTEBOOK_AI_API_KEY wins,
    # then OPENNOTEBOOK_AI_KEY, then OPENROUTER_API_KEY.
    ai_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "OPENNOTEBOOK_AI_API_KEY", "OPENNOTEBOOK_AI_KEY", "OPENROUTER_API_KEY"
        ),
    )
    embed_model: str = ""

    # How a request is tied to a person. `local` signs every request without a
    # key in as one owner, for a studio run by one person on their own machine;
    # `keys` asks every request for an API key. Sign-in for people is open
    # (docs/open-questions.md), and lands here as a third mode.
    auth: Literal["local", "keys"] = "local"
    local_owner_email: str = "owner@localhost"

    # `local` signs in anyone who can reach the api, so serving it beyond this
    # machine is refused unless the operator says they mean it.
    allow_local_auth_on_network: bool = False

    # Where the web app is served from in development, for CORS.
    web_origin: str = "http://localhost:5173"

    # The database connections one process keeps: this many open, up to
    # `db_max_overflow` more at a busy moment, and a request that finds none
    # free waits `db_pool_timeout_s` before it is answered with an error.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_s: float = 30.0
    # Enforced by Postgres itself: a statement running longer than this is
    # cancelled, and a connection left inside an open transaction this long
    # is closed, so one stuck request cannot hold rows locked for everyone.
    # 0 turns either off. Work that needs longer sets its own with SET LOCAL.
    db_statement_timeout_s: float = 30.0
    db_idle_in_transaction_timeout_s: float = 60.0

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        for plain in ("postgresql://", "postgres://"):
            if url.startswith(plain):
                return "postgresql+psycopg://" + url[len(plain) :]
        return url


@lru_cache
def settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
