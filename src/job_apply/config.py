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


@dataclass(frozen=True)
class TelegramSettings:
    """Telegram bot configuration (M1, issue #14).

    Attributes:
        bot_token: The bot token issued by `@BotFather`. There is no
            default — the entry point (``apply-pilot-bot``) refuses to
            start without it because an empty token would silently
            produce 404s against ``api.telegram.org``.
        polling_timeout: Long-poll timeout (seconds) for ``getUpdates``.
            Defaults to 30 seconds, which matches the recommended
            maximum for Telegram long polling.

    Environment variables:

    * ``TELEGRAM_BOT_TOKEN`` (required)
    * ``TELEGRAM_POLLING_TIMEOUT`` (optional, default ``30``)
    """

    bot_token: str
    polling_timeout: int = 30

    def __post_init__(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "TelegramSettings.bot_token must be a non-empty string; "
                "set the TELEGRAM_BOT_TOKEN environment variable."
            )
        if self.polling_timeout <= 0:
            raise ValueError("TelegramSettings.polling_timeout must be a positive integer")


def get_telegram_settings() -> TelegramSettings:
    """Build TelegramSettings from the environment.

    Raises:
        ValueError: If ``TELEGRAM_BOT_TOKEN`` is unset or empty. The
            check is intentionally eager so misconfiguration surfaces
            at process start, not at the first failed HTTP call.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN environment variable must be set to a non-empty value."
        )
    raw_timeout = os.getenv("TELEGRAM_POLLING_TIMEOUT", "30")
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            f"TELEGRAM_POLLING_TIMEOUT must be an integer (got {raw_timeout!r})."
        ) from exc
    return TelegramSettings(bot_token=token, polling_timeout=timeout)
