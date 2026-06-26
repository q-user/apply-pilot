"""Shared fakes for hh_apply tests — DI-first per apply-pilot VSA + audit slice pattern.

NO real hh.ru calls. NO `Mock`-overuse. Tests use InMemoryCookieJar, FakeClock,
and httpx.MockTransport (built-in, no extra dep).
"""
from __future__ import annotations

from typing import Callable, Iterator, Optional, Tuple

import httpx
import pytest

from apply_pilot.features.hh_apply.client import HHApplyClient
from apply_pilot.features.hh_apply.config import HHApplySettings


ALLOWED_DOMAINS: Tuple[str, ...] = ("hh.ru", "hh.kz", "hh.uz")


class InMemoryCookieJar:
    """Minimal cookie store; matches allowlist-of-hh.* filter from purge_non_hh_cookies().

    Used in tests as a stand-in for MozillaCookieJar without filesystem I/O.
    """

    def __init__(self, allow: Tuple[str, ...] = ALLOWED_DOMAINS) -> None:
        self._allow = allow
        self._cookies: dict[Tuple[str, str, str], str] = {}

    @staticmethod
    def _domain_allowed(domain: str, allow: Tuple[str, ...]) -> bool:
        d = domain.lstrip(".")
        return any(d == h or d.endswith("." + h) for h in allow)

    def set(self, domain: str, path: str, name: str, value: str) -> None:
        if not self._domain_allowed(domain, self._allow):
            return
        self._cookies[(domain, path, name)] = value

    def get(self, name: str, domain: Optional[str] = None) -> Optional[str]:
        # Match HHApplyClient.cookies.get() pattern: optional domain filter.
        candidates = [
            v for (d, p, n), v in self._cookies.items()
            if n == name and (domain is None or d == domain)
        ]
        return candidates[0] if candidates else None


class FakeClock:
    """Manual-advance monotonic clock for deterministic latency assertions."""

    def __init__(self) -> None:
        self._t = 0.0

    def advance(self, ms: float) -> None:
        self._t += ms / 1000.0

    def monotonic(self) -> float:
        return self._t


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def default_settings() -> HHApplySettings:
    return HHApplySettings(
        user_agent="ru.hh.android/test-fake (Android; 14; Test)",
        xsrf_init_url="https://hh.ru/",
        timeout_seconds=10.0,
    )


@pytest.fixture
def make_transport_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], HHApplyClient]:
    """Factory returning HHApplyClient backed by httpx.MockTransport.

    Pass a handler that returns httpx.Response per request; this keeps tests in-memory.
    """
    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> HHApplyClient:
        transport = httpx.MockTransport(handler)
        return HHApplyClient(transport=transport)
    return _factory
