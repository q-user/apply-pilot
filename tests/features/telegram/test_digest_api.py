"""Tests for the digest API router and the ``DigestSettings`` config helper.

The router is exercised with ``TestClient`` and a stubbed
:class:`DigestSender` so the HTTP contract — path, method, response
shape — is pinned without spinning up the database or a real Telegram
client. The settings helper is exercised with a temporary environment
so the validation surface stays under test.
"""

from __future__ import annotations

import importlib
import os
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_apply.config import DigestSettings, get_digest_settings
from job_apply.features.telegram.digest import DigestSender

# ---------------------------------------------------------------------------
# Test-only dependency override
# ---------------------------------------------------------------------------


class _StubDigestSender:
    """Returns a canned ``(sent, on_date)`` for every ``send_to_all_users`` call."""

    def __init__(self, *, sent: int = 7) -> None:
        self.sent = sent
        self.calls: list[date | None] = []

    async def send_to_all_users(self, *, on_date: date | None = None, **_kw: Any) -> int:
        self.calls.append(on_date)
        return self.sent


def _build_test_app() -> tuple[FastAPI, _StubDigestSender]:
    """Build a minimal FastAPI app that mounts the digest router with a stub sender."""
    from job_apply.features.telegram.digest import api as digest_api

    app = FastAPI()
    app.include_router(digest_api.router)
    stub = _StubDigestSender()
    app.dependency_overrides[digest_api.get_digest_sender] = lambda: stub
    return app, stub


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_post_digest_send_returns_count_and_date() -> None:
    """``POST /digest/send`` returns the dispatch count and the digest date."""
    app, stub = _build_test_app()
    client = TestClient(app)

    response = client.post("/digest/send")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sent"] == 7
    assert payload["on_date"] == date.today().isoformat()
    assert len(stub.calls) == 1


def test_post_digest_send_uses_dependency_injection() -> None:
    """The route must build its sender through FastAPI's DI, not module globals."""
    app, stub = _build_test_app()
    client = TestClient(app)

    # A second request must hit the same stub (DI cache).
    client.post("/digest/send")
    assert len(stub.calls) == 1
    client.post("/digest/send")
    assert len(stub.calls) == 2


# ---------------------------------------------------------------------------
# DigestSettings
# ---------------------------------------------------------------------------


def test_get_digest_settings_default_is_nine() -> None:
    """``APP_DIGEST_HOUR_UTC`` defaults to 9 to match the issue's documented behaviour."""
    # Reload the config module with the env var unset so the helper
    # reads the default branch.
    prior = os.environ.pop("APP_DIGEST_HOUR_UTC", None)
    try:
        import job_apply.config as config_module

        importlib.reload(config_module)
        settings = config_module.get_digest_settings()
        assert settings.digest_hour_utc == 9
    finally:
        if prior is not None:
            os.environ["APP_DIGEST_HOUR_UTC"] = prior
        import job_apply.config as config_module  # noqa: F811

        importlib.reload(config_module)


def test_get_digest_settings_honours_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured hour is read from the environment."""
    monkeypatch.setenv("APP_DIGEST_HOUR_UTC", "13")
    assert get_digest_settings().digest_hour_utc == 13


def test_get_digest_settings_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-range hour raises ``ValueError`` at config load time."""
    monkeypatch.setenv("APP_DIGEST_HOUR_UTC", "24")
    with pytest.raises(ValueError, match="must be in"):
        get_digest_settings()


def test_get_digest_settings_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer hour raises ``ValueError`` at config load time."""
    monkeypatch.setenv("APP_DIGEST_HOUR_UTC", "noon")
    with pytest.raises(ValueError, match="must be an integer"):
        get_digest_settings()


def test_digest_settings_dataclass_validates_range() -> None:
    """The dataclass itself rejects out-of-range values, not just the helper."""
    with pytest.raises(ValueError, match="must be in"):
        DigestSettings(digest_hour_utc=-1)
    with pytest.raises(ValueError, match="must be in"):
        DigestSettings(digest_hour_utc=24)


def test_digest_sender_is_exported() -> None:
    """Smoke test: the digest sender can be imported through the package root."""
    # The ``__init__`` re-exports ``DigestSender``; importing it from
    # the package root keeps the public surface coherent.
    from job_apply.features.telegram.digest import DigestSender as _DigestSender

    assert _DigestSender is DigestSender
