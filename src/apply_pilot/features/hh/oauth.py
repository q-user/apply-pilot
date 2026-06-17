"""hh.ru OAuth2 authorization-code flow (M2, issue #19).

This module owns the OAuth client contract, the CSRF state store, and
the orchestrating service that ties the OAuth handshake to the
encrypted credential store.

The actors and their responsibilities:

* :class:`HhOAuthClient` - the protocol implemented by the production
  HTTP client and by the in-memory test fake. Splits the three
  endpoints of the OAuth2 authorization-code flow (build the
  authorize URL, exchange the code, refresh the access token).
* :class:`HhHttpOAuthClient` - production implementation backed by
  :class:`httpx.AsyncClient`. Tests inject an :class:`httpx.MockTransport`
  so no real network traffic is generated.
* :class:`InMemoryHhOAuthClient` - pre-loadable fake keyed by code and
  refresh token, used by service-level tests.
* :class:`HhOAuthStateStore` - process-local store for the CSRF state
  token issued by ``start_authorization`` and consumed by
  ``handle_callback``. One-time use is enforced at validation time.
* :class:`HhAuthService` - orchestrates the slice: generates the
  authorize URL, validates the callback, persists the resulting
  tokens via :class:`HHCredentialService`, and refreshes expired
  access tokens.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from apply_pilot.features.hh.service import HHCredentialService
from apply_pilot.shared.errors import NotFoundError, ValidationError

# hh.ru's OAuth2 endpoints. Kept as module-level constants so the
# ``authorization_url`` builder does not have to be an ``async`` method
# and tests can compare against a known value.
HH_AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
HH_TOKEN_URL = "https://hh.ru/oauth/token"


# ---------------------------------------------------------------------------
# HhTokenResponse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HhTokenResponse:
    """A normalized hh.ru token-endpoint response.

    The wire format is a JSON object with ``access_token``,
    ``refresh_token``, ``token_type``, ``expires_in`` (seconds), and
    optionally ``scope``. ``refresh_token`` may be absent (some scopes
    do not include offline access).

    The dataclass computes ``expires_at`` once at construction time
    (cached via :func:`functools.cached_property`-style field) so the
    rest of the slice can safely compare it against multiple
    timestamps without the wall clock advancing between reads.
    """

    access_token: str
    refresh_token: str | None
    token_type: str
    expires_in: int
    scope: str | None
    expires_at: datetime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # ``frozen=True`` means we cannot assign through ``self``; use
        # ``object.__setattr__`` to populate the derived field once.
        object.__setattr__(
            self,
            "expires_at",
            datetime.now(UTC) + timedelta(seconds=self.expires_in),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidOAuthStateError(ValidationError):
    """The OAuth callback's ``state`` did not match a known pending state.

    Subclasses :class:`ValidationError` so the HTTP layer can return
    a 400 without inventing a new error category. The state parameter
    is the only thing tying a callback to a known user, so an unknown
    state is treated as a hard failure.
    """

    code: str = "invalid_oauth_state"


class MissingRefreshTokenError(ValidationError):
    """A token refresh was attempted but the stored credentials lack
    a refresh token (hh.ru does not return a refresh token on every
    authorization)."""

    code: str = "missing_refresh_token"


class OAuthExchangeError(Exception):
    """The OAuth server returned an unexpected response.

    Wraps ``httpx``/transport-level errors and HTTP non-2xx responses
    uniformly so the calling service does not have to distinguish
    them. Carries the raw status code for logging.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# HhOAuthClient protocol
# ---------------------------------------------------------------------------


class HhOAuthClient(Protocol):
    """The three OAuth operations the slice relies on.

    ``authorization_url`` is local (no I/O); the other two are async
    because the production implementation talks to hh.ru over HTTP.
    """

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the URL the user should be redirected to in order to
        start the OAuth handshake."""
        ...

    async def exchange_code(self, *, code: str, redirect_uri: str) -> HhTokenResponse:
        """Exchange an authorization code for an access token."""
        ...

    async def refresh_tokens(self, *, refresh_token: str) -> HhTokenResponse:
        """Exchange a refresh token for a fresh access token (and,
        typically, a new refresh token)."""
        ...


# ---------------------------------------------------------------------------
# HhHttpOAuthClient
# ---------------------------------------------------------------------------


class HhHttpOAuthClient:
    """Production OAuth client backed by :class:`httpx.AsyncClient`.

    The transport is injectable so tests can plug in
    :class:`httpx.MockTransport`; production wiring passes ``None``
    and a real network client is used. The class does not manage the
    client lifecycle: callers are expected to either pass a long-lived
    client via ``http_client`` or let this class create a one-shot
    client per request. ``aclose()`` releases the underlying client
    iff this instance owns it.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.MockTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._default_redirect_uri = redirect_uri
        self._owns_client = http_client is None
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.AsyncClient(transport=transport, timeout=30.0)

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the hh.ru authorize URL.

        The :class:`HhAuthService` typically passes the configured
        ``redirect_uri`` so the caller does not have to know it. Tests
        pass a custom ``redirect_uri`` to verify the parameter is
        round-tripped correctly.
        """
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "state": state,
            "redirect_uri": redirect_uri,
        }
        return f"{HH_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> HhTokenResponse:
        """POST to ``/oauth/token`` with ``grant_type=authorization_code``."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        return await self._post_token(data)

    async def refresh_tokens(self, *, refresh_token: str) -> HhTokenResponse:
        """POST to ``/oauth/token`` with ``grant_type=refresh_token``."""
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
        }
        return await self._post_token(data)

    async def _post_token(self, data: dict[str, str]) -> HhTokenResponse:
        """POST the given form body to the token endpoint and parse it."""
        try:
            response = await self._http.post(HH_TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise OAuthExchangeError(
                f"failed to reach hh.ru token endpoint: {exc}", status_code=None
            ) from exc
        if response.status_code != 200:
            raise OAuthExchangeError(
                f"hh.ru token endpoint returned HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - httpx raises ValueError on bad JSON
            raise OAuthExchangeError(f"hh.ru token endpoint returned non-JSON body: {exc}") from exc
        return _parse_token_payload(payload)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance owns it."""
        if self._owns_client:
            await self._http.aclose()

    def __repr__(self) -> str:
        return (
            f"HhHttpOAuthClient(client_id={self._client_id!r}, "
            f"redirect_uri={self._default_redirect_uri!r}, "
            f"client_secret=REDACTED)"
        )


def _parse_token_payload(payload: dict[str, Any]) -> HhTokenResponse:
    """Build an :class:`HhTokenResponse` from a JSON body.

    Tolerates missing ``refresh_token`` (some scopes do not return
    one) and missing ``scope``. Raises :class:`OAuthExchangeError`
    when the payload is so broken that no access token can be
    extracted.
    """
    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise OAuthExchangeError(f"hh.ru token response missing 'access_token': {payload!r}")
    try:
        expires_in = int(payload.get("expires_in", 0))
    except (TypeError, ValueError) as exc:
        raise OAuthExchangeError(
            f"hh.ru token response has non-integer 'expires_in': {payload!r}"
        ) from exc
    return HhTokenResponse(
        access_token=access_token,
        refresh_token=payload.get("refresh_token"),
        token_type=str(payload.get("token_type", "bearer")),
        expires_in=expires_in,
        scope=payload.get("scope"),
    )


# ---------------------------------------------------------------------------
# InMemoryHhOAuthClient (tests)
# ---------------------------------------------------------------------------


class InMemoryHhOAuthClient:
    """In-memory OAuth client for unit tests.

    Pre-load token responses by code and by refresh token:

    >>> client = InMemoryHhOAuthClient()
    >>> client.queue_exchange("AUTH-CODE", HhTokenResponse(...))

    If a code/refresh_token is consumed twice, the second call raises
    :class:`OAuthExchangeError` - that mirrors real hh.ru behaviour
    (codes are one-time use, refresh tokens are rotated).

    The ``client_id`` is exposed only so the URL builder can be checked
    in tests; the real OAuth handshake does not happen through this
    fake.
    """

    def __init__(
        self,
        *,
        client_id: str = "INMEMORY",
        exchange_responses: dict[str, HhTokenResponse] | None = None,
        refresh_responses: dict[str, HhTokenResponse] | None = None,
    ) -> None:
        self._client_id = client_id
        self._exchange: dict[str, HhTokenResponse] = dict(exchange_responses or {})
        self._refresh: dict[str, HhTokenResponse] = dict(refresh_responses or {})
        self.exchange_calls: list[tuple[str, str]] = []
        self.refresh_calls: list[str] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the same URL :class:`HhHttpOAuthClient` would build."""
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "state": state,
            "redirect_uri": redirect_uri,
        }
        return f"{HH_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> HhTokenResponse:
        """Return the pre-loaded token response for *code*; the
        entry is consumed on use."""
        self.exchange_calls.append((code, redirect_uri))
        response = self._exchange.pop(code, None)
        if response is None:
            raise OAuthExchangeError(f"no pre-loaded response for code {code!r}")
        return response

    async def refresh_tokens(self, *, refresh_token: str) -> HhTokenResponse:
        """Return the pre-loaded token response for *refresh_token*;
        the entry is consumed on use."""
        self.refresh_calls.append(refresh_token)
        response = self._refresh.pop(refresh_token, None)
        if response is None:
            raise OAuthExchangeError(f"no pre-loaded response for refresh_token {refresh_token!r}")
        return response


# ---------------------------------------------------------------------------
# HhOAuthStateStore
# ---------------------------------------------------------------------------


class HhOAuthStateStore:
    """In-memory store for OAuth CSRF state tokens.

    States are one-time use: a successful ``validate_state`` removes
    the entry. Unknown / consumed / never-issued states all yield
    ``None`` so the callback handler can branch on a single value
    without distinguishing "unknown" from "expired".

    A :class:`threading.Lock` guards the internal dict; the slice is
    HTTP-driven so a coarse lock is sufficient and matches the style
    used by :mod:`apply_pilot.features.telegram.linking`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, uuid.UUID] = {}

    def generate_state(self, *, user_id: uuid.UUID) -> str:
        """Issue a fresh state token bound to *user_id*.

        The token is 32 bytes of entropy encoded as a URL-safe base64
        string (43 chars). Generating a state for a user that already
        has one does *not* invalidate the prior state - a user may
        have multiple authorize flows in flight across tabs; the
        callback handler relies on the one-time-use contract to keep
        replay attacks impossible.
        """
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._states[token] = user_id
        return token

    def validate_state(self, state: str) -> uuid.UUID | None:
        """Return the user_id bound to *state* and consume the state.

        Returns ``None`` for unknown, consumed, or never-issued
        states. Never raises.
        """
        with self._lock:
            return self._states.pop(state, None)


# ---------------------------------------------------------------------------
# HhAuthService
# ---------------------------------------------------------------------------


class HhAuthService:
    """Orchestrates the OAuth2 handshake against the credential store.

    Dependencies are constructor-injected so the slice can be tested
    end-to-end with fakes: an :class:`InMemoryHhOAuthClient`, an
    :class:`HhOAuthStateStore`, and an :class:`HHCredentialService`
    wired to an in-memory repository and a real Fernet encryptor.

    ``start_authorization`` is purely local (no I/O) and synchronous.
    ``handle_callback`` and ``refresh_user_token`` are async because
    they talk to hh.ru; the public surface exposes them as ``async``
    methods so FastAPI can call them directly without an extra
    event-loop bridge.
    """

    def __init__(
        self,
        *,
        oauth_client: HhOAuthClient,
        state_store: HhOAuthStateStore,
        credential_service: HHCredentialService,
        client_id: str,
        redirect_uri: str,
    ) -> None:
        self._oauth_client = oauth_client
        self._state_store = state_store
        self._credential_service = credential_service
        # ``client_id`` and ``redirect_uri`` are kept on the service so
        # ``start_authorization`` can build the URL through the
        # injected OAuth client (whose contract is the only place
        # those parameters are defined).
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    # ------------------------------------------------------------------
    # Authorize
    # ------------------------------------------------------------------

    def start_authorization(self, *, user_id: uuid.UUID) -> dict[str, str]:
        """Issue a CSRF state and build the authorize URL.

        Returns a dict with ``authorization_url`` (where the user
        should be redirected) and ``state`` (the CSRF token bound to
        the authorize flow; the callback handler does not need to
        read it, but exposing it lets the caller correlate logs).
        """
        state = self._state_store.generate_state(user_id=user_id)
        url = self._oauth_client.authorization_url(
            state=state,
            redirect_uri=self._redirect_uri,
        )
        return {"authorization_url": url, "state": state}

    # ------------------------------------------------------------------
    # Callback
    # ------------------------------------------------------------------

    async def handle_callback(self, *, code: str, state: str) -> dict[str, Any]:
        """Validate the state, exchange the code, persist the tokens.

        Raises:
            InvalidOAuthStateError: If *state* does not match a
                pending authorize flow. The code is not exchanged
                in that case.
            OAuthExchangeError: If the token endpoint fails.
        """
        user_id = self._state_store.validate_state(state)
        if user_id is None:
            raise InvalidOAuthStateError("OAuth state is unknown, expired, or already consumed")
        response = await self._oauth_client.exchange_code(
            code=code,
            redirect_uri=self._redirect_uri,
        )
        redacted = self._credential_service.store_credentials(
            user_id=user_id,
            access_token=response.access_token,
            refresh_token=response.refresh_token,
            token_type=response.token_type,
            expires_at=response.expires_at,
        )
        return _redacted_to_dict(redacted, user_id=user_id)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def refresh_user_token(self, *, user_id: uuid.UUID) -> dict[str, Any]:
        """Read the stored refresh token, exchange it, persist the result.

        Raises:
            NotFoundError: If the user has no stored credentials.
            MissingRefreshTokenError: If the stored credentials do
                not carry a refresh token.
            OAuthExchangeError: If the token endpoint fails.
        """
        try:
            current = self._credential_service.get_credentials(user_id)
        except NotFoundError as exc:
            raise NotFoundError.for_entity("HH credentials", str(user_id)) from exc
        if not current.refresh_token:
            raise MissingRefreshTokenError(
                f"user {user_id} has no refresh token; re-authorize required"
            )
        response = await self._oauth_client.refresh_tokens(
            refresh_token=current.refresh_token,
        )
        redacted = self._credential_service.store_credentials(
            user_id=user_id,
            access_token=response.access_token,
            refresh_token=response.refresh_token,
            token_type=response.token_type,
            expires_at=response.expires_at,
        )
        return _redacted_to_dict(redacted, user_id=user_id)


def _redacted_to_dict(redacted: Any, *, user_id: uuid.UUID) -> dict[str, Any]:
    """Convert a :class:`RedactedCredentials` DTO into a plain dict
    that mirrors the wire format the API layer will return.

    The DTO's ``model_dump(mode="json")`` returns ``token_type``,
    ``expires_at`` and the redacted placeholders; we add ``user_id``
    as a string for client convenience.
    """
    body = redacted.model_dump(mode="json")
    body["user_id"] = str(user_id)
    return body


__all__ = [
    "HH_AUTHORIZE_URL",
    "HH_TOKEN_URL",
    "HhAuthService",
    "HhHttpOAuthClient",
    "HhOAuthClient",
    "HhOAuthStateStore",
    "HhTokenResponse",
    "InMemoryHhOAuthClient",
    "InvalidOAuthStateError",
    "MissingRefreshTokenError",
    "OAuthExchangeError",
]
