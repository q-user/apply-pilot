"""Telegram bot dispatcher.

A thin wrapper around the Telegram Bot API used to translate incoming
``Update`` payloads into :class:`SendMessageRequest` responses. The
dispatcher is intentionally a pure function — :meth:`TelegramBot.handle_update`
does no I/O — so the rules of every command live in one place and can be
exercised end-to-end from a unit test without touching the network.

The HTTP transport (``httpx.AsyncClient``) is created lazily on first use and
can be injected at construction time for tests or alternative transports.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

import httpx

from apply_pilot.config import TelegramSettings
from apply_pilot.features.messaging.dto import SendMessageRequest
from apply_pilot.features.messaging.protocols import MessagingAccountRepository
from apply_pilot.features.telegram.linking import (
    InvalidLinkingTokenError,
    TelegramAccountAlreadyLinkedError,
    TelegramLinkingService,
)
from apply_pilot.features.telegram.repository import TelegramAccountRepository

# Action handler imports are deferred to the _handle_*_command methods to
# break a circular import: ``messaging.actions.accept`` → ``matches.service``
# → ``apply_worker.notifications`` → ``telegram.repository`` → ``telegram``
# → ``telegram.bot`` → ``messaging.actions.accept``. The annotations on the
# constructor parameters stay as strings (``from __future__ import annotations``
# + the TYPE_CHECKING block below) so the import order does not matter.


if TYPE_CHECKING:
    from apply_pilot.features.messaging.actions.accept import AcceptActionHandler
    from apply_pilot.features.messaging.actions.defer import DeferActionHandler
    from apply_pilot.features.messaging.actions.regenerate import (
        RegenerateActionHandler,
    )
    from apply_pilot.features.messaging.actions.reject import RejectActionHandler
    from apply_pilot.features.messaging.actions.review import ReviewActionHandler

# Telegram Bot API base URL. Token is appended per request, not baked into the
# client, so the same client can be reused if the bot token is rotated
# (mostly relevant for tests; production wiring is single-token).
_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramBot:
    """A thin dispatcher for the Telegram Bot API.

    The class owns the bot's settings and an optional injected HTTP client.
    HTTP access is centralised on :meth:`_get_client` so a future swap to
    ``aiogram`` or a stubbed transport can be done in one place.
    """

    def __init__(
        self,
        settings: TelegramSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        linking_service: TelegramLinkingService | None = None,
        telegram_account_repository: TelegramAccountRepository | None = None,
        account_repo: MessagingAccountRepository | None = None,
        accept_handler: AcceptActionHandler | None = None,
        defer_handler: DeferActionHandler | None = None,
        regenerate_handler: RegenerateActionHandler | None = None,
        reject_handler: RejectActionHandler | None = None,
        review_handler: ReviewActionHandler | None = None,
    ) -> None:
        self._settings = settings
        # Keep the injected reference but do not eagerly create a client:
        # ``handle_update`` is pure and tests construct ``TelegramBot``
        # without intending to make any network calls.
        self._injected_client = http_client
        self._owned_client: httpx.AsyncClient | None = None
        # Optional linking service: when injected, /link command becomes
        # active; when None, /link returns a "not available" message.
        self._linking_service = linking_service
        # Optional repository for persisting linked TelegramAccount rows.
        # Telegram-specific (the linking command creates a TelegramAccount
        # row with ``telegram_user_id``); the channel-agnostic account
        # repo for the action handlers is the separate ``account_repo``.
        self._telegram_account_repository = telegram_account_repository
        # Channel-agnostic account repo for the action handlers. Stored
        # here for symmetry with the other handler injection points;
        # the action handlers receive it through their own constructor
        # kwargs (set by the caller, typically ``process.py``).
        self._account_repo = account_repo
        # Optional accept action handler. When None, the ``/accept``
        # command returns a "not available" message; the link between
        # the dispatcher and the action is dependency-injected so the
        # bot stays usable in test rigs that only exercise
        # non-action commands.
        self._accept_handler = accept_handler
        # Optional defer action handler. When None, the ``/defer``
        # command returns a "not available" message; the link between
        # the dispatcher and the action is dependency-injected so the
        # bot stays usable in test rigs that only exercise
        # non-action commands.
        self._defer_handler = defer_handler
        # Optional regenerate action handler. When None, the ``/regenerate``
        # command returns a "not available" message; the link between
        # the dispatcher and the action is dependency-injected so the
        # bot stays usable in test rigs that only exercise
        # non-action commands. The handler is async because it calls
        # the LLM; ``handle_update`` is therefore async too.
        self._regenerate_handler = regenerate_handler
        # Optional reject action handler. When None, the ``/reject``
        # command returns a "not available" message; the link between
        # the dispatcher and the action is dependency-injected so the
        # bot stays usable in test rigs that only exercise
        # non-action commands.
        self._reject_handler = reject_handler
        # Optional review action handler. When None, the ``/review``
        # command returns a "not available" message; the link between
        # the dispatcher and the action is dependency-injected so the
        # bot stays usable in test rigs that only exercise
        # non-action commands.
        self._review_handler = review_handler

    @property
    def settings(self) -> TelegramSettings:
        return self._settings

    @property
    def _api_base(self) -> str:
        return f"{_TELEGRAM_API_BASE}/bot{self._settings.bot_token}"

    def _method_url(self, method: str) -> str:
        return f"{self._api_base}/{method}"

    def _get_client(self) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._owned_client is None:
            # The HTTP timeout is a hair longer than the long-poll timeout
            # so a slow Telegram server never aborts a healthy poll.
            self._owned_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.polling_timeout + 5.0)
            )
        return self._owned_client

    async def aclose(self) -> None:
        """Close the bot's owned HTTP client, if any.

        Safe to call multiple times. Injected clients are owned by the
        caller and left untouched.
        """
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    # ------------------------------------------------------------------
    # HTTP transport (used by TelegramBotProcess)
    # ------------------------------------------------------------------
    async def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        """Call ``getUpdates`` and return the ``result`` array.

        Raises:
            httpx.HTTPError: on any non-2xx response or transport error.
        """
        params: dict[str, Any] = {"timeout": self._settings.polling_timeout}
        if offset is not None:
            params["offset"] = offset
        response = await self._get_client().get(self._method_url("getUpdates"), params=params)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", [])
        # The Telegram API guarantees a list here, but the cast is needed
        # because ``dict.get`` widens the value to ``Any`` and ``isinstance``
        # alone does not narrow the element type for the type checker.
        if isinstance(result, list):
            return cast("list[dict[str, Any]]", result)
        return []

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """Call ``sendMessage`` and return the result body.

        Raises:
            httpx.HTTPError: on any non-2xx response or transport error.
        """
        response = await self._get_client().post(
            self._method_url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", {})
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------
    async def handle_update(self, update: dict[str, Any]) -> SendMessageRequest | None:
        """Parse an incoming ``Update`` and return the reply to send.

        Returns ``None`` for updates the bot does not act on (non-message
        updates, messages without text, messages without a chat id, ...)
        so the caller can skip them without a special branch. The function
        is ``async`` because the :class:`RegenerateActionHandler`
        (M4, issue #40) is async — it calls the LLM. All other
        handlers are sync but the dispatcher awaits them uniformly
        so the call sites stay symmetric.

        The function is intentionally side-effect free: it never
        touches the network and never reads from the environment.
        """
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return None

        text = (message.get("text") or "").strip()
        if not text:
            return None

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return None

        command = self._extract_command(text)
        if command is None:
            # Plain text is silently ignored by the skeleton. Future slices
            # can route free-form input through a conversation handler.
            return None

        if command == "start":
            return SendMessageRequest(chat_id=chat_id, text=self._welcome_text())
        if command == "help":
            return SendMessageRequest(chat_id=chat_id, text=self._help_text())
        if command == "link":
            return self._handle_link_command(
                chat_id=chat_id,
                telegram_user_id=message.get("from", {}).get("id", 0),
                message_text=text,
                telegram_username=(message.get("from") or {}).get("username"),
            )
        if command == "accept":
            return self._handle_accept_command(
                chat_id=chat_id,
                telegram_user_id=message.get("from", {}).get("id", 0),
                message_text=text,
            )
        if command == "defer":
            return self._handle_defer_command(
                chat_id=chat_id,
                telegram_user_id=message.get("from", {}).get("id", 0),
                message_text=text,
            )
        if command == "regenerate":
            return await self._handle_regenerate_command(
                chat_id=chat_id,
                telegram_user_id=message.get("from", {}).get("id", 0),
                message_text=text,
            )
        if command == "reject":
            return self._handle_reject_command(
                chat_id=chat_id,
                telegram_user_id=message.get("from", {}).get("id", 0),
                message_text=text,
            )
        if command == "review":
            return self._handle_review_command(
                chat_id=chat_id,
                telegram_user_id=message.get("from", {}).get("id", 0),
                message_text=text,
            )
        return SendMessageRequest(chat_id=chat_id, text=self._fallback_text())

    @staticmethod
    def _extract_command_args(text: str) -> tuple[str, str]:
        """Return (command_name, args_string) from a command text.

        ``args_string`` is the whitespace-stripped remainder after the
        command token, or an empty string.
        """
        body = text[1:]  # strip leading '/'
        parts = body.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        return command, args

    @staticmethod
    def _extract_command(text: str) -> str | None:
        """Return the lower-cased command name from a text message.

        Telegram commands look like ``/start`` or ``/start@botname`` in
        group chats. Anything else is treated as plain text and ignored.
        """
        if not text.startswith("/"):
            return None
        # Split into command and trailing arguments, then strip the optional
        # ``@botname`` suffix that Telegram appends in group contexts.
        first_token = text[1:].split(maxsplit=1)[0]
        command = first_token.split("@", 1)[0]
        if not command:
            return None
        return command.lower()

    @staticmethod
    def _welcome_text() -> str:
        return (
            "Welcome to Apply Pilot! 👋\n\n"
            "To link your Telegram account, open the web app and use the "
            "one-time deep-link token from your settings page "
            "(`<DEEP_LINK_TOKEN>` placeholder).\n\n"
            "Type /help to see available commands."
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "Available commands:\n"
            "/start — show welcome message and account-linking hint\n"
            "/link — link your Telegram account using the code from the web app\n"
            "/accept <match_id> — mark one of your matches as accepted\n"
            "/defer <match_id> — shelve one of your matches for later\n"
            "/regenerate <match_id> — ask the LLM for a fresh cover letter\n"
            "/reject <match_id> [reason] — mark one of your matches as rejected\n"
            "/review <match_id> — render a vacancy review card for one of your matches\n"
            "/help — list available commands"
        )

    def _handle_link_command(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        message_text: str,
        telegram_username: str | None = None,
    ) -> SendMessageRequest:
        """Handle the /link command: validate and link a Telegram account."""
        if self._linking_service is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text="Account linking is not available right now. Please try again later.",
            )

        _cmd, args = self._extract_command_args(message_text)
        if not args:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "Usage: /link <code>\n\n"
                    "Get your linking code from the web app (Settings → Telegram)."
                ),
            )

        try:
            user_id_str = self._linking_service.link_account(
                token=args, telegram_user_id=telegram_user_id
            )
        except TelegramAccountAlreadyLinkedError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "❌ This Telegram account is already linked to another user.\n\n"
                    "Please contact support if you believe this is an error."
                ),
            )
        except InvalidLinkingTokenError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "❌ Invalid or expired linking code.\n\n"
                    "Please request a new code from the web app and try again."
                ),
            )

        # Persist the TelegramAccount row if a repository is available.
        if self._telegram_account_repository is not None:
            try:
                self._telegram_account_repository.create(
                    user_id=uuid.UUID(user_id_str),
                    telegram_user_id=telegram_user_id,
                    username=telegram_username,
                )
            except Exception:
                return SendMessageRequest(
                    chat_id=chat_id,
                    text=(
                        "❌ Failed to save your account link. "
                        "Please try again later or contact support."
                    ),
                )

        return SendMessageRequest(
            chat_id=chat_id,
            text=(
                "✅ Your Telegram account has been linked successfully! "
                "You will now receive job alerts and updates here."
            ),
        )

    def _handle_accept_command(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/accept <match_id>`` command.

        The handler is collaborator-injected. When no handler is wired
        (e.g. the bot is running with a stripped-down set of
        dependencies for local hacking) the command returns a
        "not available" message instead of crashing.
        """
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.accept import (
            ACCEPT_HELP_TEXT,
            parse_accept_command,
        )

        if self._accept_handler is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=("Accept action is not available right now. Please try again later."),
            )

        command = parse_accept_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=ACCEPT_HELP_TEXT)

        return self._accept_handler.handle(
            chat_id=chat_id,
            messaging_user_id=telegram_user_id,
            command=command,
        )

    def _handle_defer_command(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/defer <match_id>`` command.

        The handler is collaborator-injected. When no handler is wired
        (e.g. the bot is running with a stripped-down set of
        dependencies for local hacking) the command returns a
        "not available" message instead of crashing.
        """
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.defer import (
            DEFER_HELP_TEXT,
            parse_defer_command,
        )

        if self._defer_handler is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=("Defer action is not available right now. Please try again later."),
            )

        command = parse_defer_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=DEFER_HELP_TEXT)

        return self._defer_handler.handle(
            chat_id=chat_id,
            messaging_user_id=telegram_user_id,
            command=command,
        )

    async def _handle_regenerate_command(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/regenerate <match_id>`` command.

        The handler is collaborator-injected. When no handler is wired
        (e.g. the bot is running with a stripped-down set of
        dependencies for local hacking) the command returns a
        "not available" message instead of crashing. The method is
        ``async`` because :class:`RegenerateActionHandler.handle`
        awaits the LLM call.
        """
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.regenerate import (
            REGENERATE_HELP_TEXT,
            parse_regenerate_command,
        )

        if self._regenerate_handler is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=("Regenerate action is not available right now. Please try again later."),
            )

        command = parse_regenerate_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=REGENERATE_HELP_TEXT)

        return await self._regenerate_handler.handle(
            chat_id=chat_id,
            messaging_user_id=telegram_user_id,
            command=command,
        )

    def _handle_reject_command(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/reject <match_id> [reason]`` command.

        The handler is collaborator-injected. When no handler is wired
        (e.g. the bot is running with a stripped-down set of
        dependencies for local hacking) the command returns a
        "not available" message instead of crashing.
        """
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.reject import (
            REJECT_HELP_TEXT,
            parse_reject_command,
        )

        if self._reject_handler is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=("Reject action is not available right now. Please try again later."),
            )

        command = parse_reject_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=REJECT_HELP_TEXT)

        return self._reject_handler.handle(
            chat_id=chat_id,
            messaging_user_id=telegram_user_id,
            command=command,
        )

    def _handle_review_command(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/review <match_id>`` command.

        The handler is collaborator-injected. When no handler is wired
        (e.g. the bot is running with a stripped-down set of
        dependencies for local hacking) the command returns a
        "not available" message instead of crashing.
        """
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.review import (
            REVIEW_HELP_TEXT,
            parse_review_command,
        )

        if self._review_handler is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=("Review action is not available right now. Please try again later."),
            )

        command = parse_review_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=REVIEW_HELP_TEXT)

        return self._review_handler.handle(
            chat_id=chat_id,
            messaging_user_id=telegram_user_id,
            match_id=command.match_id,
        )

    @staticmethod
    def _fallback_text() -> str:
        return "Sorry, I didn't recognise that command. Type /help to see what I can do."


__all__ = ["SendMessageRequest", "TelegramBot", "TelegramSettings"]
