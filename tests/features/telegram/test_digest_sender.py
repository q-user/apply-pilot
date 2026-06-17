"""Tests for :class:`DigestSender` — Telegram message dispatch for digests.

The sender is exercised with a fake :class:`TelegramBot` (recording
``send_message`` calls in a list) and a fake :class:`StatsService`
(returning canned :class:`UserStats` objects). No Mock, no network.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import pytest

from apply_pilot.features.telegram.bot import TelegramSettings
from apply_pilot.features.telegram.digest import DigestSender, UserStats
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTelegramBot:
    """Records every ``send_message`` call so tests can assert against them."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        self.calls.append((chat_id, text))
        return {"ok": True, "result": {"message_id": len(self.calls)}}


class _FakeStatsService:
    """Canned stats keyed by user id; records the last ``on_date`` requested."""

    def __init__(self, by_user: dict[uuid.UUID, UserStats]) -> None:
        self._by_user = by_user
        self.requested: list[tuple[uuid.UUID, date | None]] = []

    async def get_user_stats(self, user_id: uuid.UUID, *, on_date: date | None = None) -> UserStats:
        self.requested.append((user_id, on_date))
        return self._by_user[user_id]

    async def get_all_users_with_telegram(self) -> list[object]:
        # Sender does not consume this method directly, but provide a stub.
        return []


def _stats(*, day: date) -> UserStats:
    return UserStats(
        matches_total=10,
        matches_new=4,
        matches_review=2,
        matches_accepted=1,
        matches_rejected=1,
        matches_applied=2,
        pending_applications=1,
        applied_today=1,
        digest_date=day,
    )


def _make_sender(
    *,
    bot: _FakeTelegramBot | None = None,
    stats: _FakeStatsService | None = None,
    telegram: InMemoryTelegramAccountRepository | None = None,
) -> DigestSender:
    return DigestSender(
        stats_service=stats or _FakeStatsService(by_user={}),
        telegram_bot=bot or _FakeTelegramBot(),  # type: ignore[arg-type]
        telegram_account_repo=telegram or InMemoryTelegramAccountRepository(),
        now=lambda: datetime(2026, 6, 15, 9, 0),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# send_to_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_user_uses_linked_telegram_chat_id() -> None:
    """``send_to_user`` must look up the user's Telegram chat id and dispatch."""
    user_id = uuid.uuid4()
    bot = _FakeTelegramBot()
    telegram = InMemoryTelegramAccountRepository()
    telegram.create(user_id=user_id, telegram_user_id=987_654_321)
    stats = _FakeStatsService(by_user={user_id: _stats(day=date(2026, 6, 15))})
    sender = _make_sender(bot=bot, stats=stats, telegram=telegram)  # type: ignore[arg-type]

    result = await sender.send_to_user(user_id)

    assert result is True
    assert len(bot.calls) == 1
    chat_id, text = bot.calls[0]
    assert chat_id == 987_654_321
    assert "2026-06-15" in text
    assert "10 total" in text


@pytest.mark.asyncio
async def test_send_to_user_returns_false_when_no_telegram_link() -> None:
    """Users without a Telegram link must be silently skipped."""
    user_id = uuid.uuid4()
    bot = _FakeTelegramBot()
    sender = _make_sender(bot=bot)  # type: ignore[arg-type]

    result = await sender.send_to_user(user_id)

    assert result is False
    assert bot.calls == []


# ---------------------------------------------------------------------------
# send_to_all_users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_all_users_dispatches_to_every_linked_user() -> None:
    """Every user with a Telegram account must receive the digest.

    Pass the user list explicitly so the test isolates the sender's
    dispatch logic from the stats service's user enumeration (the
    fake stats service returns an empty list for that method on
    purpose — its job is to fake ``get_user_stats``).
    """
    bot = _FakeTelegramBot()
    telegram = InMemoryTelegramAccountRepository()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    telegram.create(user_id=user_a, telegram_user_id=11)
    telegram.create(user_id=user_b, telegram_user_id=22)
    stats = _FakeStatsService(
        by_user={
            user_a: _stats(day=date(2026, 6, 15)),
            user_b: _stats(day=date(2026, 6, 15)),
        }
    )
    sender = _make_sender(bot=bot, stats=stats, telegram=telegram)  # type: ignore[arg-type]

    sent = await sender.send_to_all_users(users=[user_a, user_b])

    assert sent == 2
    chat_ids = {call[0] for call in bot.calls}
    assert chat_ids == {11, 22}


@pytest.mark.asyncio
async def test_send_to_all_users_skips_users_without_telegram() -> None:
    """A user that exists but has no Telegram link is skipped, not failed."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    bot = _FakeTelegramBot()
    telegram = InMemoryTelegramAccountRepository()
    telegram.create(user_id=user_a, telegram_user_id=11)
    # user_b has no linked account.
    stats = _FakeStatsService(
        by_user={user_a: _stats(day=date(2026, 6, 15)), user_b: _stats(day=date(2026, 6, 15))}
    )
    sender = _make_sender(bot=bot, stats=stats, telegram=telegram)  # type: ignore[arg-type]

    # Pass the candidate user list explicitly so the test does not depend on
    # the stats service's ``get_all_users_with_telegram`` implementation.
    sent = await sender.send_to_all_users(users=[user_a, user_b])

    assert sent == 1
    assert bot.calls[0][0] == 11


@pytest.mark.asyncio
async def test_send_to_all_users_returns_zero_when_no_users() -> None:
    """No candidates → zero dispatches, no exceptions."""
    sender = _make_sender()
    assert await sender.send_to_all_users(users=[]) == 0


@pytest.mark.asyncio
async def test_send_to_all_users_renders_digest_with_correct_date() -> None:
    """The rendered message must reflect the ``on_date`` parameter."""
    user_id = uuid.uuid4()
    bot = _FakeTelegramBot()
    telegram = InMemoryTelegramAccountRepository()
    telegram.create(user_id=user_id, telegram_user_id=42)
    target = date(2026, 12, 31)
    stats = _FakeStatsService(by_user={user_id: _stats(day=target)})
    sender = _make_sender(bot=bot, stats=stats, telegram=telegram)  # type: ignore[arg-type]

    sent = await sender.send_to_all_users(users=[user_id], on_date=target)

    assert sent == 1
    assert "2026-12-31" in bot.calls[0][1]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_digest_sender_is_a_value_object() -> None:
    """DigestSender keeps its collaborators on simple attributes."""
    bot = _FakeTelegramBot()
    stats = _FakeStatsService(by_user={})
    sender = DigestSender(
        stats_service=stats,  # type: ignore[arg-type]
        telegram_bot=bot,  # type: ignore[arg-type]
        telegram_account_repo=InMemoryTelegramAccountRepository(),
        now=lambda: datetime(2026, 6, 15, 9, 0),  # type: ignore[arg-type]
    )
    # No DB queries happen on construction; this is a smoke test.
    assert sender.telegram_bot is bot
    assert sender.stats_service is stats
    _ = TelegramSettings(bot_token="t", polling_timeout=30)  # exercise import-time wiring
