"""Tests for the MAX bot dispatcher.

Covers the pure dispatch logic in
:meth:`apply_pilot.features.max.bot.MaxBot.handle_update` and the static
parser :meth:`MaxBot._extract_command`. The HTTP transport is never
touched: a real :class:`httpx.AsyncClient` is only created lazily on
first use, so constructing a ``MaxBot`` with test settings does not
open any sockets.

Action handlers are mocked with :class:`unittest.mock.Mock` /
:class:`unittest.mock.AsyncMock` so the dispatcher is exercised in
isolation — the goal is to pin the rules of every command, not the
match-service integration. The async handler
(:class:`RegenerateActionHandler`) uses :class:`AsyncMock` because the
bot awaits its result; the sync handlers use :class:`Mock` with a
``return_value`` so the call site sees a real :class:`SendMessageRequest`.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from apply_pilot.config import MaxSettings
from apply_pilot.features.max.bot import MaxBot
from apply_pilot.features.max.linking import MaxLinkingService
from apply_pilot.features.max.repository import InMemoryMaxAccountRepository
from apply_pilot.features.messaging.actions.accept import AcceptActionHandler
from apply_pilot.features.messaging.actions.defer import DeferActionHandler
from apply_pilot.features.messaging.actions.regenerate import RegenerateActionHandler
from apply_pilot.features.messaging.actions.reject import RejectActionHandler
from apply_pilot.features.messaging.actions.review import ReviewActionHandler
from apply_pilot.features.messaging.dto import SendMessageRequest
from apply_pilot.runtime.process import BaseProcess

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _settings() -> MaxSettings:
    """Return a default :class:`MaxSettings` for tests."""
    return MaxSettings(bot_token="test-token", polling_timeout=30)


def _max_update(
    text: str,
    *,
    chat_id: int = 100,
    max_user_id: int = 100,
) -> dict[str, Any]:
    """Build a minimal MAX update carrying a text message from a user.

    Mirrors the shape produced by the live MAX API: a top-level
    ``update_type`` discriminator, a ``body.text`` payload, and the
    recipient / sender envelope.
    """
    return {
        "update_type": "message_created",
        "message": {
            "body": {"text": text},
            "recipient": {"chat_id": chat_id, "type": "dialog"},
            "sender": {"user_id": max_user_id, "is_bot": False, "first_name": "Alice"},
        },
    }


def _non_message_update(*, update_type: str = "message_callback") -> dict[str, Any]:
    """Build a MAX update whose ``update_type`` is not ``message_created``."""
    return {"update_type": update_type, "payload": {"callback_data": "noop"}}


def _make_bot() -> tuple[MaxBot, dict[str, Any]]:
    """Build a :class:`MaxBot` with mocked action handlers + account repo.

    Returns the bot plus a ``stubs`` dict that exposes the five action
    handlers by name so tests can assert against their call history.
    """
    stubs = {
        "accept": Mock(spec=AcceptActionHandler),
        "defer": Mock(spec=DeferActionHandler),
        "regenerate": AsyncMock(spec=RegenerateActionHandler),
        "reject": Mock(spec=RejectActionHandler),
        "review": Mock(spec=ReviewActionHandler),
    }
    # Configure the canned return values so the bot can build a
    # ``SendMessageRequest`` reply for each command.
    for name, stub in stubs.items():
        if isinstance(stub, AsyncMock):
            stub.handle = AsyncMock(  # type: ignore[method-assign]
                return_value=SendMessageRequest(chat_id=100, text=f"{name}-ok")
            )
        else:
            stub.handle = Mock(  # type: ignore[method-assign]
                return_value=SendMessageRequest(chat_id=100, text=f"{name}-ok")
            )

    return (
        MaxBot(
            settings=_settings(),
            account_repo=InMemoryMaxAccountRepository(),
            linking_service=MaxLinkingService(),
            accept_handler=stubs["accept"],  # type: ignore[arg-type]
            defer_handler=stubs["defer"],  # type: ignore[arg-type]
            reject_handler=stubs["reject"],  # type: ignore[arg-type]
            review_handler=stubs["review"],  # type: ignore[arg-type]
            regenerate_handler=stubs["regenerate"],  # type: ignore[arg-type]
        ),
        stubs,
    )


# ---------------------------------------------------------------------------
# _extract_command parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", "start"),
        ("/help", "help"),
        ("/link abc123", "link"),
        ("/review 11111111-1111-1111-1111-111111111111", "review"),
        ("/accept 11111111-1111-1111-1111-111111111111", "accept"),
        ("/defer 11111111-1111-1111-1111-111111111111", "defer"),
        ("/reject 11111111-1111-1111-1111-111111111111 salary too low", "reject"),
        ("/regenerate 11111111-1111-1111-1111-111111111111", "regenerate"),
        ("/notacommand", "notacommand"),
        # Group-chat ``@botname`` suffix is stripped.
        ("/start@somebot", "start"),
        ("/help@somebot", "help"),
        # Non-commands return None.
        ("hello world", None),
        ("", None),
    ],
)
def test_extract_command_parses_all_supported_commands(text: str, *, expected: str | None) -> None:
    """``_extract_command`` parses the lower-cased command name (or ``None``)."""
    assert MaxBot._extract_command(text) == expected  # noqa: SLF001 — test-only wiring


# ---------------------------------------------------------------------------
# handle_update — /start and /help
# ---------------------------------------------------------------------------


async def test_handle_start_command_returns_welcome() -> None:
    """``/start`` returns a :class:`SendMessageRequest` with the welcome copy."""
    bot, _stubs = _make_bot()

    response = await bot.handle_update(_max_update("/start"))

    assert response is not None
    assert response.chat_id == 100
    assert "Welcome" in response.text
    assert "MAX" in response.text
    assert "/help" in response.text


async def test_handle_help_command_lists_commands() -> None:
    """``/help`` lists every command the bot understands."""
    bot, _stubs = _make_bot()

    response = await bot.handle_update(_max_update("/help"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text
    assert "/start" in text
    assert "/link" in text
    assert "/accept" in text
    assert "/defer" in text
    assert "/regenerate" in text
    assert "/reject" in text
    assert "/review" in text


# ---------------------------------------------------------------------------
# handle_update — /link
# ---------------------------------------------------------------------------


async def test_handle_link_command_with_valid_code_returns_success() -> None:
    """``/link <code>`` with a valid code returns a success message."""
    bot, _stubs = _make_bot()
    linking = bot._linking_service  # noqa: SLF001 — test-only wiring
    token = linking.generate_token(user_id=str(uuid.uuid4()))

    response = await bot.handle_update(_max_update(f"/link {token}"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text.lower()
    assert "linked" in text or "success" in text


async def test_handle_link_command_persists_max_account() -> None:
    """A successful ``/link`` persists a :class:`MaxAccount` row."""
    bot, _stubs = _make_bot()
    linking = bot._linking_service  # noqa: SLF001
    user_id = uuid.uuid4()
    token = linking.generate_token(user_id=str(user_id))

    await bot.handle_update(_max_update(f"/link {token}", chat_id=555, max_user_id=555))

    persisted = bot._account_repo.find_by_max_user_id(555)  # noqa: SLF001
    assert persisted is not None
    assert persisted.user_id == user_id
    assert persisted.max_user_id == 555


async def test_handle_link_command_with_invalid_code_returns_error() -> None:
    """``/link <bad code>`` returns an error message (no account persisted)."""
    bot, _stubs = _make_bot()

    response = await bot.handle_update(_max_update("/link garbage-code"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text.lower()
    assert "invalid" in text or "expired" in text or "error" in text
    # No row was persisted for the unknown sender.
    assert bot._account_repo.find_by_max_user_id(100) is None  # noqa: SLF001


async def test_handle_link_command_without_code_returns_usage_hint() -> None:
    """``/link`` without a code returns a usage hint."""
    bot, _stubs = _make_bot()

    response = await bot.handle_update(_max_update("/link"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text.lower()
    assert "usage" in text or "code" in text


# ---------------------------------------------------------------------------
# handle_update — non-message_created envelopes
# ---------------------------------------------------------------------------


async def test_handle_update_ignores_non_message_created_envelopes() -> None:
    """``update_type != "message_created"`` is a no-op (v1 contract)."""
    bot, stubs = _make_bot()

    response = await bot.handle_update(_non_message_update(update_type="message_callback"))

    assert response is None
    # No action handler should have been invoked.
    for stub in stubs.values():
        stub.handle.assert_not_called()  # type: ignore[attr-defined]


async def test_handle_update_ignores_missing_message_body() -> None:
    """An update without a ``message`` dict is a no-op."""
    bot, _stubs = _make_bot()

    response = await bot.handle_update({"update_type": "message_created"})

    assert response is None


async def test_handle_update_ignores_plain_text_messages() -> None:
    """Plain (non-command) text is silently ignored by the skeleton dispatcher."""
    bot, _stubs = _make_bot()

    response = await bot.handle_update(_max_update("hello world"))

    assert response is None


# ---------------------------------------------------------------------------
# handle_update — action handlers
# ---------------------------------------------------------------------------


async def test_handle_accept_command_invokes_accept_handler() -> None:
    """``/accept <match_id>`` routes to the accept handler with parsed command."""
    bot, stubs = _make_bot()
    match_id = uuid.uuid4()

    response = await bot.handle_update(_max_update(f"/accept {match_id}"))

    assert response is not None
    assert response.text == "accept-ok"
    stubs["accept"].handle.assert_called_once()  # type: ignore[attr-defined]
    # The handler receives the parsed command + the chat/messaging user ids.
    call = stubs["accept"].handle.call_args  # type: ignore[attr-defined]
    assert call.kwargs["chat_id"] == 100
    assert call.kwargs["messaging_user_id"] == 100
    assert call.kwargs["command"].match_id == match_id


async def test_handle_defer_command_invokes_defer_handler() -> None:
    """``/defer <match_id>`` routes to the defer handler."""
    bot, stubs = _make_bot()
    match_id = uuid.uuid4()

    response = await bot.handle_update(_max_update(f"/defer {match_id}"))

    assert response is not None
    assert response.text == "defer-ok"
    stubs["defer"].handle.assert_called_once()  # type: ignore[attr-defined]
    call = stubs["defer"].handle.call_args  # type: ignore[attr-defined]
    assert call.kwargs["command"].match_id == match_id


async def test_handle_reject_command_invokes_reject_handler() -> None:
    """``/reject <match_id> <reason>`` routes to the reject handler."""
    bot, stubs = _make_bot()
    match_id = uuid.uuid4()

    response = await bot.handle_update(_max_update(f"/reject {match_id} salary too low"))

    assert response is not None
    assert response.text == "reject-ok"
    stubs["reject"].handle.assert_called_once()  # type: ignore[attr-defined]


async def test_handle_review_command_invokes_review_handler() -> None:
    """``/review <match_id>`` routes to the review handler."""
    bot, stubs = _make_bot()
    match_id = uuid.uuid4()

    response = await bot.handle_update(_max_update(f"/review {match_id}"))

    assert response is not None
    assert response.text == "review-ok"
    stubs["review"].handle.assert_called_once()  # type: ignore[attr-defined]
    call = stubs["review"].handle.call_args  # type: ignore[attr-defined]
    assert call.kwargs["match_id"] == match_id


async def test_handle_regenerate_command_awaits_async_handler() -> None:
    """``/regenerate <match_id>`` awaits the async regenerate handler."""
    bot, stubs = _make_bot()
    match_id = uuid.uuid4()

    response = await bot.handle_update(_max_update(f"/regenerate {match_id}"))

    assert response is not None
    assert response.text == "regenerate-ok"
    # The async mock is awaited by the bot.
    stubs["regenerate"].handle.assert_awaited_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# /extract_command_args (helper for /link)
# ---------------------------------------------------------------------------


def test_extract_command_args_returns_command_and_args() -> None:
    """``_extract_command_args`` returns ``(command, args)`` split on whitespace."""
    cmd, args = MaxBot._extract_command_args("/link abc123")  # noqa: SLF001

    assert cmd == "link"
    assert args == "abc123"


def test_extract_command_args_handles_no_args() -> None:
    """``_extract_command_args`` returns an empty ``args`` string for ``/cmd`` alone."""
    cmd, args = MaxBot._extract_command_args("/start")  # noqa: SLF001

    assert cmd == "start"
    assert args == ""


# ---------------------------------------------------------------------------
# Text chunking (MAX API hard limit is 4000 chars)
# ---------------------------------------------------------------------------


def test_split_text_returns_single_chunk_for_short_text() -> None:
    """A short text round-trips unchanged."""
    chunks = MaxBot._split_text("hello world", 4000)  # noqa: SLF001

    assert chunks == ["hello world"]


def test_split_text_splits_at_paragraph_boundaries() -> None:
    """A text longer than ``limit`` is split at ``\\n\\n`` boundaries."""
    body_a = "a" * 3000
    body_b = "b" * 3000
    text = body_a + "\n\n" + body_b

    chunks = MaxBot._split_text(text, 4000)  # noqa: SLF001

    # Each chunk respects the limit.
    assert all(len(c) <= 4000 for c in chunks)
    # The original text is fully reconstructible.
    assert "\n\n".join(chunks) == text


def test_split_text_hard_cuts_oversized_paragraph() -> None:
    """A single paragraph longer than ``limit`` is hard-cut (no mid-word split)."""
    text = "x" * 8500

    chunks = MaxBot._split_text(text, 4000)  # noqa: SLF001

    assert all(len(c) <= 4000 for c in chunks)
    # The total length survives intact.
    assert sum(len(c) for c in chunks) == 8500


def test_split_text_empty_input_yields_empty_chunk() -> None:
    """``""`` produces ``[""]`` so the caller's ``for chunk in chunks`` still fires once."""
    chunks = MaxBot._split_text("", 4000)  # noqa: SLF001

    assert chunks == [""]


# ---------------------------------------------------------------------------
# Process integration (smoke)
# ---------------------------------------------------------------------------


def test_max_bot_process_module_imports() -> None:
    """The process module is importable and the class is a :class:`BaseProcess` subclass."""
    from apply_pilot.features.max.process import MaxBotProcess

    assert issubclass(MaxBotProcess, BaseProcess)
