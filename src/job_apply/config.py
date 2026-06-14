"""Application settings."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite+pysqlite:///./app.db"),
    )


@dataclass(frozen=True)
class RedisSettings:
    """Connection settings for the async Redis client.

    Defaults target a local development server. Production deployments
    construct an instance (or subclass) directly to override ``redis_url`` and
    ``redis_db``. The runtime feature is the only consumer in M0.
    """

    redis_url: str = "redis://localhost:6379/0"
    redis_db: int = 0
    decode_responses: bool = True
    socket_timeout: float | None = None
    retry_on_timeout: bool = False
