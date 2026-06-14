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


@dataclass(frozen=True)
class DatabaseSettings:
    """Database connection settings (SQLAlchemy-oriented).

    Read `DATABASE_URL` from the environment when present; otherwise fall back
    to a local sqlite file suitable for development.
    """

    database_url: str = "sqlite:///./dev.db"
    pool_size: int = 5
    max_overflow: int = 10
    pool_pre_ping: bool = True
    echo: bool = False


def get_database_settings() -> DatabaseSettings:
    """Build DatabaseSettings from the environment, honoring DATABASE_URL."""
    return DatabaseSettings(database_url=os.getenv("DATABASE_URL", "sqlite:///./dev.db"))


# --- Resume settings (M1, issues #15 and #16) -------------------------------
# Append-only: existing settings/dataclasses above are left untouched. The
# resumes slice reads ``APP_RESUMES_MAX_FILE_SIZE_MB`` to know the per-upload
# hard limit; defaulting to 10 MB matches the issue spec.
_RESUMES_DEFAULT_MAX_FILE_SIZE_MB = 10


@dataclass(frozen=True)
class ResumeSettings:
    """Configuration for the resumes vertical slice.

    Only the size limit is environment-driven for now; the content-type
    allow-list is intentionally a Python constant on ``ResumesService``
    because changing it is a code-level decision, not an ops one.
    """

    max_file_size_mb: int = _RESUMES_DEFAULT_MAX_FILE_SIZE_MB

    @property
    def max_file_size_bytes(self) -> int:
        """Maximum permitted upload size, in bytes."""
        return self.max_file_size_mb * 1024 * 1024


def get_resume_settings() -> ResumeSettings:
    """Build ResumeSettings from the environment, honoring APP_RESUMES_MAX_FILE_SIZE_MB."""
    raw = os.getenv("APP_RESUMES_MAX_FILE_SIZE_MB", str(_RESUMES_DEFAULT_MAX_FILE_SIZE_MB))
    try:
        max_mb = int(raw)
    except ValueError as exc:
        raise ValueError(f"APP_RESUMES_MAX_FILE_SIZE_MB must be an integer; got {raw!r}") from exc
    if max_mb <= 0:
        raise ValueError(f"APP_RESUMES_MAX_FILE_SIZE_MB must be a positive integer; got {max_mb}")
    return ResumeSettings(max_file_size_mb=max_mb)
