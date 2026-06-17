"""TDD tests for the apply-worker notification slice (M5, issue #50).

When an :class:`~apply_pilot.features.apply_worker.models.ApplyJob`
reaches a terminal state, the user gets a Telegram message. The
:class:`ApplyNotifier` Protocol is the seam; the production
implementation is :class:`TelegramApplyNotifier`; tests use recording
fakes for both the notifier and the bot.

The notifier is sync from the caller's point of view — the protocol's
:meth:`ApplyNotifier.notify` returns ``None`` — but it bridges to the
async :meth:`TelegramBot.send_message` internally so a sync service
method can invoke it after persisting.

Test surface
------------

The 10 test cases cover:

* :meth:`TelegramApplyNotifier.notify` with ``status="succeeded"``
  sends the ✅ confirmation carrying the short job id.
* :meth:`TelegramApplyNotifier.notify` with ``status="failed"`` sends
  the ❌ retry hint including ``last_error``.
* :meth:`TelegramApplyNotifier.notify` with ``status="dead_letter"``
  sends the 💀 exhaustion message including ``attempts``.
* :meth:`TelegramApplyNotifier.notify` with ``status="cancelled"``
  sends the 🚫 cancellation message.
* :meth:`TelegramApplyNotifier.notify` silently skips users without a
  linked Telegram account (no ``send_message`` call).
* :meth:`TelegramApplyNotifier.notify` resolves the chat id from the
  linked :class:`TelegramAccount` row.
* :meth:`ApplyJobService.complete` triggers a ``succeeded`` notification.
* :meth:`ApplyJobService.fail` triggers a ``failed`` notification.
* :meth:`ApplyJobService.cancel` triggers a ``cancelled`` notification.
* :class:`ApplyJobService` without a notifier does not call any
  notification sink (backward compatibility).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from apply_pilot.features.apply_worker.models import ApplyJob, ApplyJobStatus
from apply_pilot.features.apply_worker.notifications import (
    ApplyNotifier,
    TelegramApplyNotifier,
)
from apply_pilot.features.apply_worker.repository import (
    InMemoryApplyJobRepository,
    InMemoryApplyStatusHistoryRepository,
)
from apply_pilot.features.apply_worker.service import ApplyJobService
from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeMatchRepo:
    """In-memory match repository exposing only :meth:`get_by_id`."""

    matches: dict[uuid.UUID, VacancyMatch] = field(default_factory=dict)

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        return self.matches.get(match_id)

    def add(self, match: VacancyMatch) -> VacancyMatch:
        self.matches[match.id] = match
        return match


@dataclass
class _FakeProfileRepo:
    """In-memory search-profile repository exposing only :meth:`get_by_id`."""

    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)

    def add(self, profile: SearchProfile) -> SearchProfile:
        self.profiles[profile.id] = profile
        return profile


class _FakeTelegramBot:
    """Recording stand-in for :class:`TelegramBot`.

    The production bot is async; the fake mirrors the async signature
    so the notifier's ``asyncio.run`` / ``create_task`` bridge works
    the same way it will in production.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        self.calls.append((chat_id, text))
        return {"message_id": len(self.calls)}


@dataclass
class _FakeNotifier:
    """Spy notifier that records every :meth:`notify` call.

    Implements the :class:`ApplyNotifier` Protocol with a sync method
    so the slice's contract matches the spec.
    """

    calls: list[tuple[uuid.UUID, str, uuid.UUID]] = field(default_factory=list)

    def notify(self, user_id: uuid.UUID, *, job: ApplyJob, status: str) -> None:
        self.calls.append((user_id, status, job.id))


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------


@dataclass
class _World:
    """Tiny in-memory world wired up for one test."""

    user_id: uuid.UUID
    profile: SearchProfile
    match: VacancyMatch
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    job_repo: InMemoryApplyJobRepository
    service: ApplyJobService


def _make_world(*, notifier: ApplyNotifier | None = None) -> _World:
    user_id = uuid.uuid4()
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.ACCEPTED.value,
    )

    match_repo = _FakeMatchRepo()
    match_repo.add(match)
    profile_repo = _FakeProfileRepo()
    profile_repo.add(profile)

    job_repo = InMemoryApplyJobRepository()
    service = ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=InMemoryApplyStatusHistoryRepository(),
        notifier=notifier,
    )

    return _World(
        user_id=user_id,
        profile=profile,
        match=match,
        match_repo=match_repo,
        profile_repo=profile_repo,
        job_repo=job_repo,
        service=service,
    )


def _make_test_job(*, user_id: uuid.UUID, last_error: str | None = None) -> ApplyJob:
    """Build a detached :class:`ApplyJob` for notifier-level tests."""
    return ApplyJob(
        id=uuid.uuid4(),
        match_id=uuid.uuid4(),
        user_id=user_id,
        vacancy_id=uuid.uuid4(),
        status=ApplyJobStatus.SUCCEEDED.value,
        attempts=3,
        last_error=last_error,
    )


# ---------------------------------------------------------------------------
# TelegramApplyNotifier — message variants
# ---------------------------------------------------------------------------


def test_notify_succeeded_sends_confirmation() -> None:
    """``status="succeeded"`` → ✅ confirmation carrying the short job id."""
    user_id = uuid.uuid4()
    account_repo = InMemoryTelegramAccountRepository()
    account_repo.create(user_id=user_id, telegram_user_id=12345)
    bot = _FakeTelegramBot()
    notifier = TelegramApplyNotifier(account_repo, bot)  # type: ignore[arg-type]
    job = _make_test_job(user_id=user_id)

    notifier.notify(user_id, job=job, status=ApplyJobStatus.SUCCEEDED.value)

    assert len(bot.calls) == 1
    chat_id, text = bot.calls[0]
    assert chat_id == 12345
    assert text.startswith("✅ Application submitted! (job: ")
    assert text.endswith(")")


def test_notify_failed_sends_error_with_retry_message() -> None:
    """``status="failed"`` → ❌ message carrying ``last_error`` and a retry hint."""
    user_id = uuid.uuid4()
    account_repo = InMemoryTelegramAccountRepository()
    account_repo.create(user_id=user_id, telegram_user_id=42)
    bot = _FakeTelegramBot()
    notifier = TelegramApplyNotifier(account_repo, bot)  # type: ignore[arg-type]
    job = _make_test_job(user_id=user_id, last_error="transient network blip")

    notifier.notify(user_id, job=job, status=ApplyJobStatus.FAILED.value)

    assert len(bot.calls) == 1
    chat_id, text = bot.calls[0]
    assert chat_id == 42
    assert text.startswith("❌ Application failed:")
    assert "transient network blip" in text
    assert "retry" in text.lower()


def test_notify_dead_letter_sends_exhaustion_message() -> None:
    """``status="dead_letter"`` → 💀 message carrying the attempt count."""
    user_id = uuid.uuid4()
    account_repo = InMemoryTelegramAccountRepository()
    account_repo.create(user_id=user_id, telegram_user_id=7)
    bot = _FakeTelegramBot()
    notifier = TelegramApplyNotifier(account_repo, bot)  # type: ignore[arg-type]
    job = _make_test_job(user_id=user_id, last_error="permanent failure")

    notifier.notify(user_id, job=job, status=ApplyJobStatus.DEAD_LETTER.value)

    assert len(bot.calls) == 1
    chat_id, text = bot.calls[0]
    assert chat_id == 7
    assert text.startswith("💀 Application gave up after ")
    assert "3" in text  # attempts
    assert "permanent failure" in text


def test_notify_cancelled_sends_cancellation() -> None:
    """``status="cancelled"`` → 🚫 cancellation message."""
    user_id = uuid.uuid4()
    account_repo = InMemoryTelegramAccountRepository()
    account_repo.create(user_id=user_id, telegram_user_id=99)
    bot = _FakeTelegramBot()
    notifier = TelegramApplyNotifier(account_repo, bot)  # type: ignore[arg-type]
    job = _make_test_job(user_id=user_id)

    notifier.notify(user_id, job=job, status=ApplyJobStatus.CANCELLED.value)

    assert len(bot.calls) == 1
    chat_id, text = bot.calls[0]
    assert chat_id == 99
    assert text.startswith("🚫 Application cancelled.")


# ---------------------------------------------------------------------------
# TelegramApplyNotifier — link resolution
# ---------------------------------------------------------------------------


def test_notify_skips_user_without_telegram_link() -> None:
    """A user with no linked Telegram account gets no message."""
    account_repo = InMemoryTelegramAccountRepository()  # empty
    bot = _FakeTelegramBot()
    notifier = TelegramApplyNotifier(account_repo, bot)  # type: ignore[arg-type]
    user_id = uuid.uuid4()
    job = _make_test_job(user_id=user_id)

    notifier.notify(user_id, job=job, status=ApplyJobStatus.SUCCEEDED.value)

    assert bot.calls == []


def test_notify_uses_telegram_account_chat_id() -> None:
    """The chat id sent to the bot is the linked ``TelegramAccount.telegram_user_id``."""
    user_id = uuid.uuid4()
    account_repo = InMemoryTelegramAccountRepository()
    account_repo.create(user_id=user_id, telegram_user_id=555_000)
    bot = _FakeTelegramBot()
    notifier = TelegramApplyNotifier(account_repo, bot)  # type: ignore[arg-type]
    job = _make_test_job(user_id=user_id)

    notifier.notify(user_id, job=job, status=ApplyJobStatus.SUCCEEDED.value)

    assert len(bot.calls) == 1
    assert bot.calls[0][0] == 555_000


# ---------------------------------------------------------------------------
# ApplyJobService — notifier integration
# ---------------------------------------------------------------------------


def test_apply_job_service_complete_triggers_notification() -> None:
    """``complete`` fires a ``succeeded`` notification with the job's ``user_id``."""
    notifier = _FakeNotifier()
    world = _make_world(notifier=notifier)
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    world.service.complete(job.id, external_application_id="hh-app-1")

    assert notifier.calls == [(world.user_id, ApplyJobStatus.SUCCEEDED.value, job.id)]


def test_apply_job_service_fail_triggers_notification() -> None:
    """``fail`` fires a ``failed`` notification (including the retryable branch)."""
    notifier = _FakeNotifier()
    world = _make_world(notifier=notifier)
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    world.service.fail(job.id, error="transient", retryable=True)

    assert notifier.calls == [(world.user_id, ApplyJobStatus.FAILED.value, job.id)]


def test_apply_job_service_cancel_triggers_notification() -> None:
    """``cancel`` fires a ``cancelled`` notification."""
    notifier = _FakeNotifier()
    world = _make_world(notifier=notifier)
    job = world.service.enqueue_for_match(world.match.id)

    world.service.cancel(job.id, user_id=world.user_id)

    assert notifier.calls == [(world.user_id, ApplyJobStatus.CANCELLED.value, job.id)]


def test_apply_job_service_without_notifier_does_nothing() -> None:
    """Backward compat: a service built without a notifier never touches one."""
    world = _make_world(notifier=None)
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    # None of the three transitions should raise; ``notifier`` is simply
    # not consulted. The contract is the original M5 behaviour.
    completed = world.service.complete(job.id, external_application_id="hh-app-1")
    assert completed.status == ApplyJobStatus.SUCCEEDED.value

    # No notifier was injected, so calling ``notify`` on the (absent)
    # notifier is not possible — the assertion is simply that the
    # service does not blow up. (The other transitions are exercised
    # by the other tests; this one covers the ``notifier=None`` path.)
    assert getattr(world.service, "_notifier", None) is None
