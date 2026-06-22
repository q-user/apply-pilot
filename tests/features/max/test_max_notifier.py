"""Tests for :class:`apply_pilot.features.max.notifier.MaxApplyNotifier`.

The notifier is the MAX-side twin of
:class:`apply_pilot.features.telegram.notifications.TelegramApplyNotifier`.
It satisfies the channel-agnostic :class:`ApplyNotifier` Protocol so
the apply worker can fan out terminal-state notifications to MAX users
without branching on the channel.

The tests use a real :class:`InMemoryMaxAccountRepository` and a
:class:`Mock`-shaped :class:`MaxBot` whose ``send_message`` is
:class:`AsyncMock`. No real network I/O happens — the bot is a pure
recorder.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from apply_pilot.features.apply_worker.models import ApplyJob, ApplyJobStatus
from apply_pilot.features.max.bot import MaxBot
from apply_pilot.features.max.notifier import MaxApplyNotifier
from apply_pilot.features.max.repository import InMemoryMaxAccountRepository

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _make_bot() -> tuple[Mock, list[tuple[int, str]]]:
    """Build a fake :class:`MaxBot` whose ``send_message`` is :class:`AsyncMock`.

    Returns the bot plus a list of ``(max_user_id, text)`` pairs the
    notifier sent. Each ``send_message`` call appends a record.
    """
    sent: list[tuple[int, str]] = []

    async def _send(chat_id: int, text: str) -> dict[str, object]:
        sent.append((chat_id, text))
        return {"message": {"message_id": len(sent)}}

    bot = Mock(spec=MaxBot)
    bot.send_message = AsyncMock(side_effect=_send)
    return bot, sent


def _make_job(user_id: uuid.UUID) -> ApplyJob:
    """Build a real :class:`ApplyJob` row for the notifier under test."""
    job = ApplyJob(
        id=uuid.uuid4(),
        match_id=uuid.uuid4(),
        user_id=user_id,
        status=ApplyJobStatus.SUCCEEDED.value,
    )
    return job


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_calls_max_bot_send_message_for_linked_user() -> None:
    """A user with a linked MAX account receives the notification."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()
    repo.create(user_id=user_id, max_user_id=987_654_321)
    bot, sent = _make_bot()
    notifier = MaxApplyNotifier(max_account_repo=repo, max_bot=bot)  # type: ignore[arg-type]
    job = _make_job(user_id)

    await notifier.notify(user_id, job=job, status=job.status)

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 987_654_321
    assert str(job.id) in text
    assert job.status in text


@pytest.mark.asyncio
async def test_notify_message_format_matches_documented_template() -> None:
    """The body is ``f\"Apply job {job.id} — {status}\"`` (issue #188)."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()
    repo.create(user_id=user_id, max_user_id=42)
    bot, sent = _make_bot()
    notifier = MaxApplyNotifier(max_account_repo=repo, max_bot=bot)  # type: ignore[arg-type]
    job = _make_job(user_id)

    await notifier.notify(user_id, job=job, status="succeeded")

    assert sent[0][1] == f"Apply job {job.id} — succeeded"


# ---------------------------------------------------------------------------
# No-link case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_is_noop_for_user_without_max_link() -> None:
    """A user with no linked MAX account does not get a notification."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()  # not in the repo
    bot, sent = _make_bot()
    notifier = MaxApplyNotifier(max_account_repo=repo, max_bot=bot)  # type: ignore[arg-type]
    job = _make_job(user_id)

    await notifier.notify(user_id, job=job, status=job.status)

    assert sent == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_swallows_send_message_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``send_message`` exceptions are caught, logged, and never re-raised."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()
    repo.create(user_id=user_id, max_user_id=1)
    bot = Mock(spec=MaxBot)
    bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))
    notifier = MaxApplyNotifier(max_account_repo=repo, max_bot=bot)  # type: ignore[arg-type]
    job = _make_job(user_id)

    with caplog.at_level(logging.ERROR, logger="apply_pilot.features.max.notifier"):
        # Must not raise.
        await notifier.notify(user_id, job=job, status=job.status)

    # The bot was called (and failed).
    bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_notifier_holds_collaborators() -> None:
    """The notifier keeps its collaborators on simple attributes for tests."""
    repo = InMemoryMaxAccountRepository()
    bot, _sent = _make_bot()
    notifier = MaxApplyNotifier(max_account_repo=repo, max_bot=bot)  # type: ignore[arg-type]

    assert notifier._max_account_repo is repo  # noqa: SLF001
    assert notifier._max_bot is bot  # noqa: SLF001
