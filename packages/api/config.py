"""
API configuration. Environment only — nothing hard-coded, no secret logged.

Mirrors the convention in packages/storage/config.py so both services read
their settings the same way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_token: str
    pool_min: int
    pool_max: int
    auto_migrate: bool
    redis_url: str

    @property
    def auth_enabled(self) -> bool:
        """No token configured means auth is off — local dev only.

        Production sets API_TOKEN. `readiness()` reports this so a deploy
        without a token is visible rather than silently open.
        """
        return bool(self.api_token)


def load() -> Settings:
    return Settings(
        database_url=_env("DATABASE_URL"),
        api_token=_env("API_TOKEN"),
        pool_min=_env_int("DB_POOL_MIN", 1),
        pool_max=_env_int("DB_POOL_MAX", 8),
        auto_migrate=_env("API_AUTO_MIGRATE", "0").strip().lower() in {"1", "true", "yes", "on"},
        redis_url=_env("REDIS_URL"),
    )
