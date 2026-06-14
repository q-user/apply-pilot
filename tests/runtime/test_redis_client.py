"""Tests for the async Redis client factory."""

from __future__ import annotations

import asyncio

from job_apply.config import RedisSettings
from job_apply.runtime.redis_client import create_redis_client


class FakeRedis:
    """Minimal in-memory Redis stand-in used for DI in tests.

    Implements only the operations exercised by the tests (``set``, ``get``,
    ``ping``) and the ``from_url`` classmethod that the production factory
    delegates to. Real ``redis.asyncio.Redis`` is not imported here so the
    test module stays decoupled from the production client.
    """

    def __init__(self, url: str = "", **kwargs: object) -> None:
        self._store: dict[str, str] = {}
        self.url = url
        self.kwargs = kwargs

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> FakeRedis:
        return cls(url=url, **kwargs)

    async def set(self, key: str, value: str) -> bool:  # noqa: A003 - mirrors Redis API
        self._store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def ping(self) -> bool:
        return True


def test_redis_client_creates_from_url() -> None:
    """Factory returns a usable client; set/get round-trip works."""
    settings = RedisSettings(redis_url="redis://localhost:6379/0")
    client = create_redis_client(settings, client_factory=FakeRedis.from_url)

    async def _roundtrip() -> None:
        assert await client.set("hello", "world") is True
        assert await client.get("hello") == "world"

    asyncio.run(_roundtrip())


def test_redis_url_parsing_from_settings() -> None:
    """Settings with redis_url='redis://localhost:6379/0' propagates to the client."""
    settings = RedisSettings(redis_url="redis://localhost:6379/0")
    assert settings.redis_url == "redis://localhost:6379/0"

    captured: dict[str, object] = {}

    def factory(url: str, **kwargs: object) -> FakeRedis:
        captured["url"] = url
        captured["db"] = kwargs.get("db")
        return FakeRedis(url=url, **kwargs)

    create_redis_client(settings, client_factory=factory)

    assert captured["url"] == "redis://localhost:6379/0"
