"""Settings read from the environment, once.

Only what an operator sets lives here: where the database and the files are,
which AI endpoint to call and with what key, how people sign in. What a person
chooses (style, voices, language) is a user setting in the database.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENNOTEBOOK_", extra="ignore")

    # `postgresql+psycopg://…`; a plain `postgresql://` URL is accepted too.
    database_url: str = Field(validation_alias="DATABASE_URL")
    files_dir: Path = Path("./data/files")

    ai_base_url: str = "https://openrouter.ai/api/v1"
    ai_key: SecretStr = SecretStr("")
    embed_model: str = ""

    # How a request is tied to a person. `local` signs every request without a
    # key in as one owner, for a studio run by one person on their own machine;
    # `keys` asks every request for an API key. Sign-in for people is open
    # (docs/open-questions.md), and lands here as a third mode.
    auth: Literal["local", "keys"] = "local"
    local_owner_email: str = "owner@localhost"

    # Where the web app is served from in development, for CORS.
    web_origin: str = "http://localhost:5173"

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
