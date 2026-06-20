"""MAX bot dispatcher (M9, issue #186).

A thin wrapper around the MAX Bot API (``https://botapi.max.ru``) used to
translate incoming update payloads into :class:`SendMessageRequest`
responses. The dispatcher is intentionally a pure function —
:meth:`MaxBot.handle_update` does no I/O — so the rules of every command
live in one place and can be exercised end-to-end from a unit test
without touching the network.

The HTTP transport (``httpx.AsyncClient``) is created lazily on first use
and can be injected at construction time for tests or alternative
transports.

Key MAX API differences vs the Telegram bot:

* Bearer-token auth via ``Authorization: <bot_token>`` header on every
  request — the token is **not** baked into the URL.
* Updates carry a top-level ``update_type`` discriminator envelope; only
  ``message_created`` carries text.
* Incoming text lives at ``message.body.text`` (not ``message.text``).
* Sender is ``message.sender.user_id``; chat (recipient) is
  ``message.recipient.chat_id``.
* Polling cursor is ``marker`` (server-assigned int64, opaque), NOT
  ``offset``.
* Max text length is 4000 chars — chunk on ``\\n\\n`` boundaries when
  longer.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import httpx

from apply_pilot.config import MaxSettings
from apply_pilot.features.max.linking import (
    InvalidMaxLinkingTokenError,
    MaxAccountAlreadyLinkedError,
    MaxLinkingService,
)
from apply_pilot.features.max.repository import MaxAccountRepository
from apply_pilot.features.messaging.dto import SendMessageRequest

# Action handler imports are deferred to the _handle_*_command methods to
# break a circular import: ``messaging.actions.accept`` → ``matches.service``
# → ``apply_worker.notifications`` → ``max.repository`` → ``max``
# → ``max.bot`` → ``messaging.actions.accept``. The annotations on the
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

# MAX text length per the Bot API. Telegram's limit is 4096; MAX is 4000.
_MAX_TEXT_LENGTH = 4000


class MaxBot:
    """A thin dispatcher for the MAX Bot API.

    The class owns the bot's settings and an optional injected HTTP client.
    HTTP access is centralised on :meth:`_get_client` so a future swap to
    a stubbed transport can be done in one place.
    """

    def __init__(
        self,
        settings: MaxSettings,
        *,
        account_repo: MaxAccountRepository,
        linking_service: MaxLinkingService,
        accept_handler: AcceptActionHandler,
        defer_handler: DeferActionHandler,
        reject_handler: RejectActionHandler,
        review_handler: ReviewActionHandler,
        regenerate_handler: RegenerateActionHandler,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        # Channel-specific repository used by ``/link`` to persist the
        # ``MaxAccount`` row after a successful token consumption.
        self._account_repo = account_repo
        # Linking service: validates and consumes one-time codes, returns
        # the local user id behind a valid token.
        self._linking_service = linking_service
        # Action handlers, channel-agnostic. The dispatcher passes
        # ``messaging_user_id=max_user_id`` so the handlers resolve the
        # local user via ``account_repo.find_by_external_user_id`` which
        # the MAX repo satisfies structurally.
        self._accept_handler = accept_handler
        self._defer_handler = defer_handler
        self._regenerate_handler = regenerate_handler
        self._reject_handler = reject_handler
        self._review_handler = review_handler
        # Keep the injected reference but do not eagerly create a client:
        # ``handle_update`` is pure and tests construct ``MaxBot`` without
        # intending to make any network calls.
        self._injected_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    @property
    def _api_base(self) -> str:
        # ``rstrip("/")`` so a trailing slash in ``MAX_API_BASE`` (legal
        # in env vars) does not produce ``…/messages`` as ``…//messages``.
        return self._settings.api_base.rstrip("/")

    def _method_url(self, method: str) -> str:
        return f"{self._api_base}/{method}"

    def _get_client(self) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._owned_client is None:
            # The HTTP timeout is a hair longer than the long-poll timeout
            # so a slow MAX server never aborts a healthy poll.
            self._owned_client = httpx.AsyncClient(
                headers={"Authorization": self._settings.bot_token},
                timeout=httpx.Timeout(self._settings.polling_timeout + 5.0),
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
    # HTTP transport (used by MaxBotProcess)
    # ------------------------------------------------------------------
    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """Call ``/messages`` and return the resulting message body.

        Long replies are chunked at paragraph boundaries (see
        :meth:`_split_text`) and sent sequentially so the user receives
        the parts in order. The return value is the last chunk's parsed
        body — earlier chunks are fire-and-forget from the caller's
        perspective.

        Raises:
            httpx.HTTPError: on any non-2xx response or transport error.
        """
        last_result: dict[str, Any] = {}
        for chunk in self._split_text(text, _MAX_TEXT_LENGTH):
            response = await self._get_client().post(
                self._method_url("messages"),
                params={"chat_id": chat_id},
                json={"text": chunk},
            )
            response.raise_for_status()
            payload = response.json()
            # The MAX API returns the new message envelope under
            # ``message`` (not ``result`` like Telegram). Defensive
            # ``isinstance`` keeps the static checker happy if MAX ever
            # changes the shape.
            message = payload.get("message", {})
            if isinstance(message, dict):
                last_result = message
        return last_result

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        types: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Call ``/updates`` and return ``(updates, new_marker)``.

        The MAX API assigns an opaque ``marker`` (int64) to every long
        poll; the caller must echo it back to receive only newer updates.
        ``new_marker`` is what the response carries — the caller stores
        it for the next poll. ``None`` markers are treated as "start
        from the current tail" by the API.

        ``types`` is an optional comma-separated filter (e.g.
        ``"message_created"``) — the dispatcher only ever cares about
        ``message_created`` but the parameter is exposed for parity with
        the API surface.

        Raises:
            httpx.HTTPError: on any non-2xx response or transport error.
        """
        params: dict[str, Any] = {"timeout": self._settings.polling_timeout}
        if marker is not None:
            params["marker"] = marker
        if types is not None:
            params["types"] = types
        response = await self._get_client().get(
            self._method_url("updates"),
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        updates_raw = payload.get("updates", [])
        # The API guarantees a list, but the cast + ``isinstance`` guard
        # keeps static checkers honest if the server ever sends
        # something unexpected.
        updates: list[dict[str, Any]] = (
            [u for u in updates_raw if isinstance(u, dict)] if isinstance(updates_raw, list) else []
        )
        new_marker = payload.get("marker")
        if not isinstance(new_marker, int):
            new_marker = None
        return updates, new_marker

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------
    async def handle_update(self, update: dict[str, Any]) -> SendMessageRequest | None:
        """Parse an incoming MAX update and return the reply to send.

        Returns ``None`` for updates the bot does not act on (non-``message_created``
        envelopes, messages without text, messages without a chat id, ...)
        so the caller can skip them without a special branch. The
        function is ``async`` because :class:`RegenerateActionHandler`
        (M4, issue #40) is async — it calls the LLM. All other handlers
        are sync but the dispatcher awaits them uniformly so the call
        sites stay symmetric.

        The function is intentionally side-effect free: it never
        touches the network and never reads from the environment.
        """
        # MAX wraps every update in a top-level ``update_type`` envelope;
        # only ``message_created`` carries text the dispatcher acts on.
        if update.get("update_type") != "message_created":
            return None

        message = update.get("message")
        if not isinstance(message, dict):
            return None

        # Incoming text lives at ``message.body.text`` in MAX (Telegram
        # uses ``message.text``). ``body`` may be absent for non-text
        # messages (images, voice, ...) which we silently ignore.
        body = message.get("body") or {}
        if not isinstance(body, dict):
            return None
        text = (body.get("text") or "").strip()
        if not text:
            return None

        # Recipient chat id (where to send the reply).
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id") if isinstance(recipient, dict) else None
        if not isinstance(chat_id, int):
            return None

        # Sender id (used as ``messaging_user_id`` by the action
        # handlers, which resolve the local user via
        # ``account_repo.find_by_external_user_id``).
        sender = message.get("sender") or {}
        max_user_id = sender.get("user_id") if isinstance(sender, dict) else None
        if not isinstance(max_user_id, int):
            max_user_id = 0

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
            return await self._handle_link_command(
                chat_id=chat_id,
                max_user_id=max_user_id,
                message_text=text,
            )
        if command == "accept":
            return self._handle_accept_command(
                chat_id=chat_id,
                max_user_id=max_user_id,
                message_text=text,
            )
        if command == "defer":
            return self._handle_defer_command(
                chat_id=chat_id,
                max_user_id=max_user_id,
                message_text=text,
            )
        if command == "regenerate":
            return await self._handle_regenerate_command(
                chat_id=chat_id,
                max_user_id=max_user_id,
                message_text=text,
            )
        if command == "reject":
            return self._handle_reject_command(
                chat_id=chat_id,
                max_user_id=max_user_id,
                message_text=text,
            )
        if command == "review":
            return self._handle_review_command(
                chat_id=chat_id,
                max_user_id=max_user_id,
                message_text=text,
            )
        return SendMessageRequest(chat_id=chat_id, text=self._fallback_text())

    @staticmethod
    def _extract_command_args(text: str) -> tuple[str, str]:
        """Return ``(command_name, args_string)`` from a command text.

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

        Bot commands look like ``/start`` or ``/start@botname`` in group
        chats. Anything else is treated as plain text and ignored. The
        ``@botname`` suffix is stripped so a single parser works for
        both private and group contexts (MAX mirrors Telegram's
        convention here).
        """
        if not text.startswith("/"):
            return None
        # Split into command and trailing arguments, then strip the optional
        # ``@botname`` suffix that MAX appends in group contexts.
        first_token = text[1:].split(maxsplit=1)[0]
        command = first_token.split("@", 1)[0]
        if not command:
            return None
        return command.lower()

    @staticmethod
    def _split_text(text: str, limit: int) -> list[str]:
        """Split ``text`` into chunks at most ``limit`` characters long.

        Splits preferentially on ``\\n\\n`` (paragraph) boundaries so
        each chunk reads as a coherent block. If a single paragraph
        exceeds ``limit``, it is hard-cut at ``limit`` characters — the
        alternative (splitting mid-word) would be uglier and no safer.

        Always returns at least one chunk for non-empty input. Empty
        input returns ``[""]`` so the caller's ``for chunk in chunks``
        loop still fires once (matching Telegram's behaviour).
        """
        if not text:
            return [""]
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        for paragraph in text.split("\n\n"):
            if not paragraph:
                continue
            if len(paragraph) <= limit:
                # Try to glue the paragraph to the previous chunk with a
                # ``\\n\\n`` separator; only do so when the combined size
                # fits under ``limit`` so we never silently overflow.
                if chunks and len(chunks[-1]) + 2 + len(paragraph) <= limit:
                    chunks[-1] = chunks[-1] + "\n\n" + paragraph
                else:
                    chunks.append(paragraph)
            else:
                # Single paragraph too long — hard-cut into ``limit``-sized
                # slices. Keep each slice independent so the caller never
                # has to re-join them.
                for start in range(0, len(paragraph), limit):
                    chunks.append(paragraph[start : start + limit])
        return chunks

    @staticmethod
    def _welcome_text() -> str:
        return (
            "Welcome to apply-pilot! 👋\n\n"
            "To link your MAX account, open the web app and use the "
            "one-time deep-link token from your settings page "
            "(`<DEEP_LINK_TOKEN>` placeholder).\n\n"
            "Type /help to see available commands."
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "Available commands:\n"
            "/start — show welcome message and account-linking hint\n"
            "/link — link your MAX account using the code from the web app\n"
            "/accept <match_id> — mark one of your matches as accepted\n"
            "/defer <match_id> — shelve one of your matches for later\n"
            "/regenerate <match_id> — ask the LLM for a fresh cover letter\n"
            "/reject <match_id> [reason] — mark one of your matches as rejected\n"
            "/review <match_id> — render a vacancy review card for one of your matches\n"
            "/help — list available commands"
        )

    @staticmethod
    def _fallback_text() -> str:
        return "Unknown command. Try /help."

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------
    async def _handle_link_command(
        self,
        *,
        chat_id: int,
        max_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/link`` command: validate and link a MAX account.

        The flow mirrors the Telegram ``/link`` command: validate the
        one-time code via :class:`MaxLinkingService`, persist the
        :class:`MaxAccount` row via :class:`MaxAccountRepository`, and
        report a friendly success / error message. The method is
        ``async`` for symmetry with the rest of the dispatcher — both
        underlying collaborators are sync today.
        """
        _cmd, args = self._extract_command_args(message_text)
        if not args:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "Usage: /link <code>\n\n"
                    "Get your linking code from the web app (Settings → MAX)."
                ),
            )

        try:
            user_id_str = self._linking_service.link_account(token=args, max_user_id=max_user_id)
        except MaxAccountAlreadyLinkedError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "❌ This MAX account is already linked to another user.\n\n"
                    "Please contact support if you believe this is an error."
                ),
            )
        except InvalidMaxLinkingTokenError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "❌ Invalid or expired linking code.\n\n"
                    "Please request a new code from the web app and try again."
                ),
            )

        # Persist the MaxAccount row. Any failure here is reported to the
        # user (rather than silently swallowed) because the token has
        # already been consumed — the bot must not appear to succeed.
        try:
            self._account_repo.create(
                user_id=uuid.UUID(user_id_str),
                max_user_id=max_user_id,
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
                "✅ Your MAX account has been linked successfully! "
                "You will now receive job alerts and updates here."
            ),
        )

    def _handle_accept_command(
        self,
        *,
        chat_id: int,
        max_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/accept <match_id>`` command."""
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.accept import (
            ACCEPT_HELP_TEXT,
            parse_accept_command,
        )

        command = parse_accept_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=ACCEPT_HELP_TEXT)

        return self._accept_handler.handle(
            chat_id=chat_id,
            messaging_user_id=max_user_id,
            command=command,
        )

    def _handle_defer_command(
        self,
        *,
        chat_id: int,
        max_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/defer <match_id>`` command."""
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.defer import (
            DEFER_HELP_TEXT,
            parse_defer_command,
        )

        command = parse_defer_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=DEFER_HELP_TEXT)

        return self._defer_handler.handle(
            chat_id=chat_id,
            messaging_user_id=max_user_id,
            command=command,
        )

    async def _handle_regenerate_command(
        self,
        *,
        chat_id: int,
        max_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/regenerate <match_id>`` command.

        Async because :class:`RegenerateActionHandler.handle` awaits the
        LLM call.
        """
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.regenerate import (
            REGENERATE_HELP_TEXT,
            parse_regenerate_command,
        )

        command = parse_regenerate_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=REGENERATE_HELP_TEXT)

        return await self._regenerate_handler.handle(
            chat_id=chat_id,
            messaging_user_id=max_user_id,
            command=command,
        )

    def _handle_reject_command(
        self,
        *,
        chat_id: int,
        max_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/reject <match_id> [reason]`` command."""
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.reject import (
            REJECT_HELP_TEXT,
            parse_reject_command,
        )

        command = parse_reject_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=REJECT_HELP_TEXT)

        return self._reject_handler.handle(
            chat_id=chat_id,
            messaging_user_id=max_user_id,
            command=command,
        )

    def _handle_review_command(
        self,
        *,
        chat_id: int,
        max_user_id: int,
        message_text: str,
    ) -> SendMessageRequest:
        """Handle the ``/review <match_id>`` command."""
        # Deferred import to break the circular dependency described at
        # the top of the module.
        from apply_pilot.features.messaging.actions.review import (
            REVIEW_HELP_TEXT,
            parse_review_command,
        )

        command = parse_review_command(message_text)
        if command is None:
            return SendMessageRequest(chat_id=chat_id, text=REVIEW_HELP_TEXT)

        return self._review_handler.handle(
            chat_id=chat_id,
            messaging_user_id=max_user_id,
            match_id=command.match_id,
        )


__all__ = ["MaxBot", "MaxSettings"]
