"""Async Redis client factory and healthcheck helper."""

from __future__ import annotations

import logging
from collections.abc import Callable

import redis.asyncio
import redis.exceptions

from job_apply.config import RedisSettings

log = logging.getLogger(__name__)

#: Signature of a callable that builds a ``redis.asyncio.Redis`` client.
#: Used for dependency injection in tests so the factory can run without a
#: real Redis server.
ClientFactory = Callable[..., redis.asyncio.Redis]


def create_redis_client(
    settings: RedisSettings,
    *,
    client_factory: ClientFactory | None = None,
) -> redis.asyncio.Redis:
    """Build an async Redis client from ``settings``.

    The factory forwards every :class:`RedisSettings` field to the underlying
    client constructor (defaults to :meth:`redis.asyncio.Redis.from_url`).
    Pass ``client_factory`` to inject a custom builder; this is how tests
    substitute an in-memory stand-in for the real client.

    The returned client opens its first connection lazily on the next issued
    command, so construction itself cannot fail because of an unreachable
    server. Use :func:`healthcheck` to verify reachability from a readiness
    probe.
    """
    factory = client_factory or redis.asyncio.Redis.from_url
    return factory(
        settings.redis_url,
        db=settings.redis_db,
        decode_responses=settings.decode_responses,
        socket_timeout=settings.socket_timeout,
        retry_on_timeout=settings.retry_on_timeout,
    )


async def healthcheck(client: redis.asyncio.Redis) -> bool:
    """Return ``True`` if the Redis server answers ``PING``.

    Connection-level errors are logged at WARNING and treated as an unhealthy
    result, so callers can poll the function from a readiness probe without
    having to handle redis-py exception types.
    """
    try:
        return bool(await client.ping())
    except (redis.exceptions.RedisError, OSError) as exc:  # pragma: no cover - defensive
        log.warning("redis healthcheck failed: %s", exc)
        return False
