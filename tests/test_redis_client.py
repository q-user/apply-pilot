"""Tests for the runtime Redis client factory."""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from job_apply.config import Settings
from job_apply.runtime.redis_client import create_redis_client


@pytest.fixture
def fake_url() -> str:
    """Return a fake Redis URL usable by fakeredis."""
    return "redis://localhost:6379/0"


@pytest.fixture
def settings(fake_url: str) -> Settings:
    """Build a Settings instance with a Redis URL."""
    return Settings(
        database_url="sqlite+pysqlite:///./app.db",
        redis_url=fake_url,
    )


def test_redis_client_creates_with_config_url(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Factory must build the client with the URL from settings."""
    captured: dict[str, object] = {}

    def fake_from_url(url: str, **kwargs: object) -> fakeredis.aioredis.FakeRedis:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fakeredis.aioredis.FakeRedis(decode_responses=bool(kwargs.get("decode_responses")))

    monkeypatch.setattr("redis.asyncio.Redis.from_url", fake_from_url)

    client = create_redis_client(settings)

    assert captured["url"] == settings.redis_url
    assert isinstance(client, fakeredis.aioredis.FakeRedis)


def test_redis_client_uses_decoding(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Factory must request decode_responses=True for ergonomic str returns."""
    captured_kwargs: dict[str, object] = {}

    def fake_from_url(url: str, **kwargs: object) -> fakeredis.aioredis.FakeRedis:
        captured_kwargs.update(kwargs)
        return fakeredis.aioredis.FakeRedis(decode_responses=bool(kwargs.get("decode_responses")))

    monkeypatch.setattr("redis.asyncio.Redis.from_url", fake_from_url)

    client = create_redis_client(settings)

    assert captured_kwargs.get("decode_responses") is True

    async def roundtrip() -> str:
        await client.set("k", "v")
        return await client.get("k")

    assert asyncio.run(roundtrip()) == "v"
