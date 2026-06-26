"""@pytest.mark.optional_local_only — single e2e smoke against live hh.ru.

This file IS skipped in CI. Run manually with::

    uv run pytest tests/features/hh_apply/test_optional_live.py -v -m optional_local_only

It exists so the developer has a reproducible smoke harness, but is intentionally
out-of-band for automated testing — hh.ru rates bots.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.optional_local_only


@pytest.mark.skip(reason="optional_local_only — run manually; never in CI")
@pytest.mark.asyncio
async def test_live_smoke_against_hh_ru() -> None:
    """Trivial GET to https://hh.ru/ root to verify network path is healthy.

    Real hh.ru negotiate/apply is not invoked here — that needs a real session
    cookie, candidate resume, and a target vacancy, and is more comprehensively
    exercised in production worker tests rather than this CI-smoke surface.
    """
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get("https://hh.ru/")
        assert response.status_code in (200, 302), "unexpected hh.ru root response"
