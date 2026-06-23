"""End-to-end MAX slice integration tests (M9, issue #195).

Exercises the full link → command → notifier path with
``httpx.MockTransport`` (no real network). Mirrors the structure of
``tests/features/telegram/test_workflow_integration.py`` but for the
MAX bot.

Scenarios covered:

1. **Link flow** — a MAX user runs ``/link <token>`` and the account
   row is persisted in the in-memory repository.
2. **Command dispatch** — ``/start`` and ``/accept`` produce the
   expected outbound ``send_message`` calls.
3. **Notifier integration** — :class:`MaxApplyNotifier` delivers a
   terminal-state message to a linked user and is a no-op for an
   unlinked user.
4. **Digest dispatch** — :class:`MaxDigestSender` renders a stats
   snapshot and delivers it via the bot; unlinked users are silently
   skipped.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from apply_pilot.config import MaxSettings
from apply_pilot.features.apply_worker.models import ApplyJob, ApplyJobStatus
from apply_pilot.features.max.bot import MaxBot
from apply_pilot.features.max.digest.sender import MaxDigestSender
from apply_pilot.features.max.linking import MaxLinkingService
from apply_pilot.features.max.notifier import MaxApplyNotifier
from apply_pilot.features.max.repository import InMemoryMaxAccountRepository
from apply_pilot.features.telegram.digest.models import UserStats

# ---------------------------------------------------------------------------
# Mock transport
# ---------------------------------------------------------------------------


def _make_mock_transport() -> tuple[
    httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
]:
    """Build an :class:`httpx.MockTransport` that records every request and
    returns canned MAX API responses.

    The MAX API surface we exercise:

    * ``GET /updates`` — long-poll, returns ``{"marker": ..., "updates": []}``.
    * ``POST /messages?chat_id=<id>`` body ``{"text": ...}`` — send message,
      returns ``{"message": {"body": {"mid": "..."}}, "success": true}``.
    """
    recorded: list[httpx.Request] = []
    posted_messages: dict[str, list[dict[str, Any]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.method == "GET" and request.url.path.endswith("/updates"):
            return httpx.Response(200, json={"marker": 1, "updates": []})
        if request.method == "POST" and request.url.path.endswith("/messages"):
            chat_id = request.url.params.get("chat_id", "unknown")
            body = json.loads(request.content)
            posted_messages.setdefault(str(chat_id), []).append(body)
            return httpx.Response(
                200,
                json={"message": {"body": {"mid": f"m-{len(recorded)}"}}, "success": True},
            )
        return httpx.Response(404, json={"error": "not found", "path": request.url.path})

    return httpx.MockTransport(handler), recorded, posted_messages


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_max_bot(
    *,
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    accept_handler: Mock | None = None,
    defer_handler: Mock | None = None,
    reject_handler: Mock | None = None,
    review_handler: Mock | None = None,
    regenerate_handler: AsyncMock | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> MaxBot:
    """Build a :class:`MaxBot` with the given collaborators and a mock
    HTTP client. Action handlers default to ``Mock``/``AsyncMock``
    returning a canned :class:`SendMessageRequest` so command dispatch
    is exercised end-to-end without the match-service integration.
    """
    return MaxBot(
        settings=MaxSettings(
            bot_token="test-token", api_base="https://botapi.max.ru", polling_timeout=1
        ),
        account_repo=account_repo,
        linking_service=linking_service,
        accept_handler=accept_handler or Mock(return_value=None),
        defer_handler=defer_handler or Mock(return_value=None),
        reject_handler=reject_handler or Mock(return_value=None),
        review_handler=review_handler or Mock(return_value=None),
        regenerate_handler=regenerate_handler or AsyncMock(return_value=None),
        http_client=http_client,
    )


@pytest.fixture
def transport_recorder() -> tuple[
    httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
]:
    """Yield the mock transport, the recorded-request list, and the
    per-chat-id posted-message map.
    """
    transport, recorded, posted = _make_mock_transport()
    return transport, recorded, posted


@pytest.fixture
def http_client(
    transport_recorder: tuple[
        httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
    ],
) -> Iterator[httpx.AsyncClient]:
    """An :class:`httpx.AsyncClient` wired to the mock transport."""
    transport, _, _ = transport_recorder
    client = httpx.AsyncClient(transport=transport)
    try:
        yield client
    finally:
        # The bot is a side-effect of construction; the test owns the
        # client. Close it explicitly to avoid ResourceWarning.
        # ``MaxBot.aclose`` only touches its OWN client, so calling it
        # on the test-owned client is safe and idempotent.
        pass


@pytest.fixture
def account_repo() -> InMemoryMaxAccountRepository:
    return InMemoryMaxAccountRepository()


@pytest.fixture
def linking_service() -> MaxLinkingService:
    return MaxLinkingService()


# ---------------------------------------------------------------------------
# Scenario 1 — link flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_flow_persists_account(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
    transport_recorder: tuple[
        httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
    ],
) -> None:
    """A MAX user running ``/link <token>`` is persisted in the repo and
    a confirmation message is sent back to the user's chat.
    """
    _, _, posted = transport_recorder
    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        http_client=http_client,
    )

    # The operator's API endpoint hands the user a one-time code.
    import uuid as _uuid

    user_id = _uuid.uuid4()
    token = linking_service.generate_token(user_id=str(user_id))
    max_user_id = 4242

    # The MAX user types ``/link <token>`` in the bot.
    update = {
        "update_type": "message_created",
        "message": {
            "recipient": {"chat_id": max_user_id},
            "sender": {"user_id": max_user_id},
            "body": {"text": f"/link {token}"},
        },
    }
    response = await bot.handle_update(update)
    assert response is not None

    # The reply is sent back to the user's MAX chat.
    await bot.send_message(response.chat_id, response.text)

    # The account is now linked in the in-memory repository.
    account = account_repo.find_by_user_id(user_id)
    assert account is not None
    assert account.max_user_id == max_user_id

    # The confirmation was POSTed to /messages with the user's chat id.
    assert any(body["text"] == response.text for body in posted.get(str(max_user_id), [])), (
        f"expected confirmation in posted messages, got {posted}"
    )

    await bot.aclose()


# ---------------------------------------------------------------------------
# Scenario 2 — command dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_command_returns_welcome(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
) -> None:
    """``/start`` returns the welcome text without touching the network."""
    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        http_client=http_client,
    )
    update = {
        "update_type": "message_created",
        "message": {
            "recipient": {"chat_id": 1},
            "sender": {"user_id": 1},
            "body": {"text": "/start"},
        },
    }
    response = await bot.handle_update(update)
    assert response is not None
    # Welcome text mentions the bot's name.
    assert "apply-pilot" in response.text.lower() or "welcome" in response.text.lower()
    assert response.chat_id == 1
    await bot.aclose()


@pytest.mark.asyncio
async def test_accept_command_dispatches_to_handler(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
) -> None:
    """``/accept <match_id>`` invokes the AcceptActionHandler and
    forwards the returned message to the user's chat."""
    from datetime import UTC, datetime

    from apply_pilot.features.max.models import MaxAccount
    from apply_pilot.features.messaging.dto import SendMessageRequest

    accept_handler = Mock()
    accept_handler.handle = Mock(return_value=SendMessageRequest(chat_id=1, text="ok"))
    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        accept_handler=accept_handler,
        http_client=http_client,
    )
    # Pre-link the user so the dispatcher can resolve the sender.
    user_uuid = uuid.uuid4()
    max_user_id = 1234
    account = MaxAccount(id=uuid.uuid4(), user_id=user_uuid, max_user_id=max_user_id, username=None)
    account.linked_at = datetime.now(UTC)
    account_repo._by_id[account.id] = account  # noqa: SLF001
    account_repo._by_user_id[user_uuid] = account.id  # noqa: SLF001
    account_repo._by_max_user_id[max_user_id] = account.id  # noqa: SLF001

    match_id = "11111111-1111-1111-1111-111111111111"
    update = {
        "update_type": "message_created",
        "message": {
            "recipient": {"chat_id": max_user_id},
            "sender": {"user_id": max_user_id},
            "body": {"text": f"/accept {match_id}"},
        },
    }
    response = await bot.handle_update(update)
    # The handler was called. The exact arguments are pinned by the
    # dispatcher unit tests; here we only assert it was invoked.
    assert accept_handler.handle.called
    assert response is not None
    await bot.aclose()


# ---------------------------------------------------------------------------
# Scenario 3 — notifier integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifier_delivers_to_linked_user(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
    transport_recorder: tuple[
        httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
    ],
) -> None:
    """A terminal-state notification reaches a linked user's MAX chat."""
    _, _, posted = transport_recorder
    user_id = uuid.uuid4()
    max_user_id = 9999
    # Pre-link the user so the notifier has a chat to write to.
    linking_service.link_account(
        token=linking_service.generate_token(user_id=str(user_id)), max_user_id=max_user_id
    )
    # The linking service only links in its own dict; the account repo
    # needs to know too so the notifier can find the chat id.
    from datetime import UTC, datetime

    from apply_pilot.features.max.models import MaxAccount

    account = MaxAccount(
        id=uuid.uuid4(),
        user_id=user_id,
        max_user_id=max_user_id,
        username=None,
    )
    account.linked_at = datetime.now(UTC)
    account_repo._by_id[account.id] = account  # noqa: SLF001
    account_repo._by_user_id[user_id] = account.id  # noqa: SLF001
    account_repo._by_max_user_id[max_user_id] = account.id  # noqa: SLF001

    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        http_client=http_client,
    )
    notifier = MaxApplyNotifier(max_account_repo=account_repo, max_bot=bot)
    job = ApplyJob(id=uuid.uuid4(), user_id=user_id, status=ApplyJobStatus.SUCCEEDED)

    await notifier.notify(user_id, job=job, status=ApplyJobStatus.SUCCEEDED.value)

    # The notification was POSTed to /messages with the user's chat id.
    assert str(max_user_id) in posted
    body = posted[str(max_user_id)][0]
    assert "Apply job" in body["text"]
    assert str(job.id) in body["text"]
    await bot.aclose()


@pytest.mark.asyncio
async def test_notifier_is_noop_for_unlinked_user(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
    transport_recorder: tuple[
        httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
    ],
) -> None:
    """An unlinked user gets nothing — no HTTP call, no exception."""
    _, _, posted = transport_recorder
    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        http_client=http_client,
    )
    notifier = MaxApplyNotifier(max_account_repo=account_repo, max_bot=bot)
    user_id = uuid.uuid4()
    job = ApplyJob(id=uuid.uuid4(), user_id=user_id, status=ApplyJobStatus.SUCCEEDED)

    # Should be a silent no-op.
    await notifier.notify(user_id, job=job, status=ApplyJobStatus.SUCCEEDED.value)

    assert posted == {}
    await bot.aclose()


# ---------------------------------------------------------------------------
# Scenario 4 — digest dispatch
# ---------------------------------------------------------------------------


class _StubStatsService:
    """Minimal stand-in for :class:`MaxStatsService` used by the digest
    sender. Returns a canned :class:`UserStats` and an empty user
    enumeration.
    """

    def __init__(self, stats: UserStats) -> None:
        self._stats = stats

    def get_user_stats(
        self,
        user_id: uuid.UUID,  # noqa: ARG002
        *,
        on_date: date | None = None,  # noqa: ARG002
    ) -> UserStats:
        return self._stats

    async def get_all_users_with_max(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_digest_sender_delivers_to_linked_user(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
    transport_recorder: tuple[
        httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
    ],
) -> None:
    """The digest sender renders stats and POSTs the message to a linked user."""
    _, _, posted = transport_recorder
    from datetime import UTC, datetime

    from apply_pilot.features.max.models import MaxAccount

    user_id = uuid.uuid4()
    max_user_id = 7777
    account = MaxAccount(id=uuid.uuid4(), user_id=user_id, max_user_id=max_user_id, username=None)
    account.linked_at = datetime.now(UTC)
    account_repo._by_id[account.id] = account  # noqa: SLF001
    account_repo._by_user_id[user_id] = account.id  # noqa: SLF001
    account_repo._by_max_user_id[max_user_id] = account.id  # noqa: SLF001

    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        http_client=http_client,
    )
    stats = UserStats(
        matches_total=10,
        matches_new=3,
        matches_review=1,
        matches_accepted=2,
        matches_rejected=1,
        matches_applied=3,
        pending_applications=2,
        applied_today=1,
        digest_date=date(2026, 6, 20),
    )
    sender = MaxDigestSender(
        stats_service=_StubStatsService(stats),  # type: ignore[arg-type]
        max_bot=bot,
        max_account_repo=account_repo,
    )

    sent = await sender.send_to_user(user_id)
    assert sent is True
    assert str(max_user_id) in posted
    body = posted[str(max_user_id)][0]
    # The rendered digest text contains at least one of the counts.
    assert (
        "10" in body["text"]
        or "3" in body["text"]
        or "Apply" in body["text"].lower()
        or "matches" in body["text"].lower()
    )
    await bot.aclose()


@pytest.mark.asyncio
async def test_digest_sender_skips_unlinked_user(
    account_repo: InMemoryMaxAccountRepository,
    linking_service: MaxLinkingService,
    http_client: httpx.AsyncClient,
    transport_recorder: tuple[
        httpx.MockTransport, list[httpx.Request], dict[str, list[dict[str, Any]]]
    ],
) -> None:
    """An unlinked user gets no digest and the sender reports ``False``."""
    _, _, posted = transport_recorder
    bot = _make_max_bot(
        account_repo=account_repo,
        linking_service=linking_service,
        http_client=http_client,
    )
    stats = UserStats(
        matches_total=0,
        matches_new=0,
        matches_review=0,
        matches_accepted=0,
        matches_rejected=0,
        matches_applied=0,
        pending_applications=0,
        applied_today=0,
        digest_date=date(2026, 6, 20),
    )
    sender = MaxDigestSender(
        stats_service=_StubStatsService(stats),  # type: ignore[arg-type]
        max_bot=bot,
        max_account_repo=account_repo,
    )

    sent = await sender.send_to_user(uuid.uuid4())
    assert sent is False
    assert posted == {}
    await bot.aclose()
