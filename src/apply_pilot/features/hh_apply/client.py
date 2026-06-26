"""Async httpx client with Android-UA defaults, allowlist cookie jar, XSRF auto-refresh on 401."""

from __future__ import annotations

import logging
from http.cookiejar import MozillaCookieJar
from typing import Any

import httpx

from .models import HHApplyError

logger = logging.getLogger(__name__)

HH_DOMAINS: tuple[str, ...] = ("hh.ru", "hh.kz", "hh.uz")

# Android UA — per docs/integrations/hh_apply.md §3. Marked CONTROLLED; T3 (HHApplySettings)
# may override this at construction time. The literal string `TBD` is replaced before T2 lands
# (we'll source the stable version from publicly-known Android app metadata, not reverse-engineer).
DEFAULT_USER_AGENT: str = "ru.hh.android/TBD (Android; 14; Pixel)"

DEFAULT_BASE_URL: str = "https://hh.ru"
NEGOTIATIONS_PATH: str = "/negotiations"


class HHApplyClient(httpx.AsyncClient):
    """Subclass of httpx.AsyncClient that wraps hh.ru mobile-fingerprint behaviour."""

    def __init__(
        self,
        cookies: MozillaCookieJar | None = None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        base_url: str = DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> None:
        # Transport headers that pin us into the Android-mobile apply flow.
        transport_headers: dict[str, str] = {
            "User-Agent": user_agent,
            "Origin": base_url,
            "Referer": base_url + "/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "X-Requested-With": "XMLHttpRequest",
        }
        # Caller-supplied headers layer on top — tooling overrides win.
        custom = kwargs.pop("headers", {}) or {}
        transport_headers.update(custom)
        kwargs["headers"] = transport_headers

        # Allow MozillaCookieJar to be reused; default to fresh MozillaCookieJar.
        jar = cookies if cookies is not None else MozillaCookieJar()
        kwargs["cookies"] = jar

        super().__init__(**kwargs)

    @staticmethod
    def _cookie_domain_allowed(domain: str) -> bool:
        d = domain.lstrip(".")
        return any(d == host or d.endswith("." + host) for host in HH_DOMAINS)

    def purge_non_hh_cookies(self) -> int:
        """Drop any cookies not matching `hh.ru` / `hh.kz` / `hh.uz`. Returns the number purged."""
        old_jar = self.cookies.jar
        new_jar = type(old_jar)()
        purged = 0
        for cookie in list(old_jar):
            if self._cookie_domain_allowed(cookie.domain):
                new_jar.set_cookie(cookie)
            else:
                purged += 1
        self.cookies.jar = new_jar
        logger.debug("hh_apply: purged %d non-hh cookies from jar", purged)
        return purged

    async def fetch_xsrf_token(self) -> str:
        """Bootstrap or refresh the `_xsrf` cookie via a fresh GET to hh_root."""
        origin = self.headers.get("Origin") or DEFAULT_BASE_URL
        logger.debug("hh_apply: fetching XSRF token from %s/", origin)
        response = await self.get(origin + "/")
        response.raise_for_status()
        xsrf = self.cookies.get("_xsrf")
        if not xsrf:
            raise HHApplyError(
                "XSRF token not found in cookies after bootstrap; session possibly blocked."
            )
        return xsrf

    async def request_with_xsrf_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request with the current `_xsrf` value; on HTTP 401, refresh + retry ONCE."""
        headers = dict(kwargs.pop("headers", {}) or {})
        xsrf_initial = self.cookies.get("_xsrf")
        if xsrf_initial:
            headers["X-XSRF-Token"] = xsrf_initial
        response = await self.request(method, url, headers=headers, **kwargs)
        if response.status_code != 401:
            return response
        # 401 with first attempt → refresh and try once more.
        logger.info("hh_apply: 401 received — refreshing XSRF and retrying once")
        try:
            await self.fetch_xsrf_token()
        except Exception as exc:
            logger.warning("hh_apply: XSRF refresh failed: %s — returning original 401", exc)
            return response
        xsrf_after = self.cookies.get("_xsrf") or ""
        headers["X-XSRF-Token"] = xsrf_after
        return await self.request(method, url, headers=headers, **kwargs)
