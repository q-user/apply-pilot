"""Application settings."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str

    # --- Redis runtime (M0, issue #8) ---
    # Defaults keep the dataclass drop-in backwards compatible for any
    # call site that only constructs ``Settings(database_url=...)``.
    redis_url: str = "redis://localhost:6379/0"
    redis_db: int = 0
    redis_password: str | None = None


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite+pysqlite:///./app.db"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_db=int(os.getenv("REDIS_DB", "0")),
        redis_password=os.getenv("REDIS_PASSWORD") or None,
    )


@dataclass(frozen=True)
class FastAPISettings:
    app_name: str
    host: str
    port: int
    log_level: str
    log_json: bool


def get_fastapi_settings() -> FastAPISettings:
    return FastAPISettings(
        app_name=os.getenv("APP_NAME", "apply-pilot"),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
        log_json=os.getenv("APP_LOG_JSON", "true").lower() in ("1", "true", "yes", "on"),
    )
