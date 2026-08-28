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
    my_draft_slot: int | None = None  # override when Sleeper assigns draft_order late (LS-32)

    # Be polite to undocumented endpoints (the daily pull).
    http_timeout_s: float = 60.0
    http_retries: int = 3
    http_delay_ms: int = Field(
        default=250, description="Pause between sequential calls to one host"
    )

    # The draft poll (LS-65): a 120 s pick clock can't absorb 60 s timeouts × 3 retries. Sleeper
    # answers in well under a second when healthy; a dead network must surface in seconds and
    # the poller's own capped backoff owns the retrying.
    draft_http_timeout_s: float = 5.0
    draft_max_backoff_s: float = 15.0

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
