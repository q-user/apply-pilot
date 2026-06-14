"""Application settings loaded via :mod:`pydantic_settings`.

The module exposes two layered configuration models:

- :class:`DatabaseSettings` — engine / session configuration, read from
  the ``db_*`` env-var namespace: ``DB_DSN``, ``DB_POOL_SIZE``,
  ``DB_POOL_PRE_PING`` and ``DB_ECHO``. The legacy ``DATABASE_URL`` env
  var is honoured as a back-compat alias for ``DB_DSN`` so existing
  tooling (Alembic's default URL, docker-compose templates, etc.) keeps
  working without changes.
- :class:`Settings` — top-level container, currently holding a single
  :class:`DatabaseSettings` instance.

Use :func:`get_settings` to obtain a fresh :class:`Settings` snapshot
(re-reading the environment on each call) or cache the result yourself.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """SQLAlchemy engine / session configuration.

    Mirrors the ``db_*`` env-var namespace. ``dsn`` also accepts the
    legacy ``DATABASE_URL`` env var as a back-compat alias.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    dsn: str = Field(
        default="sqlite+pysqlite:///./app.db",
        validation_alias=AliasChoices("db_dsn", "DATABASE_URL"),
    )
    pool_size: int = Field(
        default=5,
        validation_alias="db_pool_size",
    )
    pool_pre_ping: bool = Field(
        default=True,
        validation_alias="db_pool_pre_ping",
    )
    echo: bool = Field(
        default=False,
        validation_alias="db_echo",
    )


class Settings(BaseSettings):
    """Top-level application settings.

    The :class:`Settings` model is intentionally small — only the
    database block lives here in M0. Future slices extend it with their
    own blocks (e.g. redis, telegram) following the same pattern.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    @property
    def database_url(self) -> str:
        """Back-compat alias for ``Settings.database.dsn``.

        Older code (and some integrations like Alembic) read
        ``Settings.database_url``; keep the attribute working while the
        rest of the codebase migrates to ``Settings.database``.
        """
        return self.database.dsn


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` snapshot from the current environment.

    A new instance is returned on every call so tests can mutate
    ``os.environ`` between calls and observe the change.
    """
    return Settings()


__all__ = ["DatabaseSettings", "Settings", "get_settings"]
