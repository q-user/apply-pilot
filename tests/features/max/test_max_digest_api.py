"""Tests for the MAX digest API router.

The router is exercised with ``TestClient`` and a stubbed
:class:`MaxDigestSender` so the HTTP contract — path, method, response
shape — is pinned without spinning up the database or a real MAX
client. Mirrors the Telegram digest API test pattern.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apply_pilot.features.max.digest import MaxDigestSender

# ---------------------------------------------------------------------------
# Test-only dependency override
# ---------------------------------------------------------------------------


class _StubMaxDigestSender:
    """Returns a canned ``sent`` for every ``send_to_all_users`` call."""

    def __init__(self, *, sent: int = 4) -> None:
        self.sent = sent
        self.calls: list[date | None] = []

    async def send_to_all_users(self, *, on_date: date | None = None, **_kw: Any) -> int:
        self.calls.append(on_date)
        return self.sent


def _build_test_app(*, sent: int = 4) -> tuple[FastAPI, _StubMaxDigestSender]:
    """Build a minimal FastAPI app that mounts the MAX digest router with a stub sender."""
    from apply_pilot.features.max.digest import api as max_digest_api

    app = FastAPI()
    app.include_router(max_digest_api.router)
    stub = _StubMaxDigestSender(sent=sent)
    app.dependency_overrides[max_digest_api.get_max_digest_sender] = lambda: stub
    return app, stub


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_post_max_digest_send_returns_count_and_date() -> None:
    """``POST /digest/max/send`` returns the dispatch count and the digest date."""
    app, stub = _build_test_app(sent=5)
    client = TestClient(app)

    response = client.post("/digest/max/send")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sent"] == 5
    assert payload["on_date"] == date.today().isoformat()
    assert len(stub.calls) == 1


def test_post_max_digest_send_returns_zero_when_no_users() -> None:
    """``sent`` echoes the configured stub count (zero in this test)."""
    app, stub = _build_test_app(sent=0)
    client = TestClient(app)

    response = client.post("/digest/max/send")

    assert response.status_code == 200
    assert response.json()["sent"] == 0
    assert len(stub.calls) == 1


def test_post_max_digest_send_returns_n_for_n_users() -> None:
    """``sent`` echoes the configured stub count (N in this test)."""
    app, stub = _build_test_app(sent=12)
    client = TestClient(app)

    response = client.post("/digest/max/send")

    assert response.status_code == 200
    assert response.json()["sent"] == 12


def test_post_max_digest_send_uses_dependency_injection() -> None:
    """The route must build its sender through FastAPI's DI, not module globals."""
    app, stub = _build_test_app(sent=2)
    client = TestClient(app)

    client.post("/digest/max/send")
    assert len(stub.calls) == 1
    client.post("/digest/max/send")
    assert len(stub.calls) == 2


def test_post_max_digest_send_response_shape_matches_pydantic_model() -> None:
    """The response is shape-compatible with :class:`MaxDigestSendResponse`."""
    app, _stub = _build_test_app(sent=1)
    client = TestClient(app)

    response = client.post("/digest/max/send")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"sent", "on_date"}
    assert isinstance(payload["sent"], int)
    assert isinstance(payload["on_date"], str)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


def test_max_digest_sender_is_exported() -> None:
    """Smoke test: the digest sender can be imported through the package root."""
    from apply_pilot.features.max.digest import MaxDigestSender as _MaxDigestSender

    assert _MaxDigestSender is MaxDigestSender


def test_router_prefix_is_digest_max() -> None:
    """The router is mounted under ``/digest/max`` so it doesn't clash with Telegram."""
    from apply_pilot.features.max.digest import api as max_digest_api

    assert max_digest_api.router.prefix == "/digest/max"
