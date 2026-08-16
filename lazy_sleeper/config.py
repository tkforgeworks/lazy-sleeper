"""Runtime configuration, loaded from environment / .env.

Everything here is a plain value; nothing does I/O at import time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://lazysleeper:lazysleeper@localhost:5433/lazysleeper"

    snapshot_dir: Path = Path("./data/snapshots")

    supabase_url: str | None = None
    supabase_secret_key: str | None = None  # sb_secret_... (legacy service_role also works)
    supabase_bucket: str = "raw-snapshots"

    sleeper_league_id: str = "1392685475625443328"
    sleeper_draft_id: str = "1392685476523024384"
    sleeper_user_id: str = "1268591266036203520"

    # Be polite to undocumented endpoints.
    http_timeout_s: float = 60.0
    http_retries: int = 3
    http_delay_ms: int = Field(
        default=250, description="Pause between sequential calls to one host"
    )

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
