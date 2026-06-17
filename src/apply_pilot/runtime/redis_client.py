"""Async Redis client factory.

Centralises how background workers obtain a Redis client so we have a
single, well-known configuration surface (URL, db, password, decode
responses) and can swap in a fake client during tests.
"""

from __future__ import annotations

from redis.asyncio import Redis

from apply_pilot.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Build an async ``redis.asyncio.Redis`` from application settings.

    Always enables ``decode_responses=True`` so callers receive ``str``
    values back from ``get``/``hget``/etc. without manual decoding.
    """
    return Redis.from_url(
        settings.redis_url,
        db=settings.redis_db,
        password=settings.redis_password,
        decode_responses=True,
    )


__all__ = ["create_redis_client"]
