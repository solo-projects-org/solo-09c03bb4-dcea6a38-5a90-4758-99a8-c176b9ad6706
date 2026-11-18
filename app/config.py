"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: str = "./data/transit.db"
    source_base_url: str = "http://127.0.0.1:8001"
    source_timeout_seconds: float = 10.0
    max_source_pages: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=os.getenv("DATABASE_PATH", "./data/transit.db"),
            source_base_url=os.getenv("SOURCE_BASE_URL", "http://127.0.0.1:8001").rstrip("/"),
            source_timeout_seconds=_env_float("SOURCE_TIMEOUT_SECONDS", 10.0),
            max_source_pages=_env_int("MAX_SOURCE_PAGES", 200),
        )
