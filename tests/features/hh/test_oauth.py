"""TDD tests for the HH OAuth2 authorization-code flow (issue #19).

These tests exercise the OAuth slice end-to-end through fakes and an
in-memory credential store:

* :class:`HhOAuthStateStore` is a process-local store for CSRF state
  tokens. The state is one-time use; an attacker who replays a stolen
  ``code`` against a previously used state must get ``None`` back.
* :class:`HhHttpOAuthClient` is the production ``httpx``-backed client.
  The tests use :class:`httpx.MockTransport` so the suite never touches
  the network while still exercising the real URL/parameter shape that
  hh.ru expects.
* :class:`HhAuthService` orchestrates: ``start_authorization`` ->
  ``handle_callback`` -> ``refresh_user_token``. The service is wired
  with a state store, an OAuth client, and an
  :class:`HHCredentialService` (in turn backed by the in-memory
  repository + a real Fernet encryptor).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from job_apply.features.hh.encryption import CredentialEncryptor
from job_apply.features.hh.oauth import (
    HhAuthService,
    HhHttpOAuthClient,
    HhOAuthStateStore,
    HhTokenResponse,
    InMemoryHhOAuthClient,
    InvalidOAuthStateError,
    MissingRefreshTokenError,
)
from job_apply.features.hh.repository import InMemoryHHCredentialRepository
from job_apply.features.hh.service import HHCredentialService
from job_apply.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# HhTokenResponse
# ---------------------------------------------------------------------------


def test_token_response_expires_at_is_now_plus_expires_in() -> None:
    """HhTokenResponse.expires_at must equal ``now() + expires_in`` (UTC)."""
    before = datetime.now(UTC)
    response = HhTokenResponse(
        access_token="acc",
        refresh_token="ref",
        token_type="bearer",
        expires_in=3600,
        scope=None,
    )
    after = datetime.now(UTC)
    expected_min = before + timedelta(seconds=3600)
    expected_max = after + timedelta(seconds=3600)
    assert expected_min <= response.expires_at <= expected_max


def test_token_response_accepts_missing_refresh_token() -> None:
    """``refresh_token`` is allowed to be ``None`` (no scope includes offline)."""
    response = HhTokenResponse(
        access_token="acc",
        refresh_token=None,
        token_type="bearer",
        expires_in=60,
        scope=None,
    )
    assert response.refresh_token is None
    assert response.token_type == "bearer"


# ---------------------------------------------------------------------------
# HhHttpOAuthClient - exercised via httpx.MockTransport
# ---------------------------------------------------------------------------


def _hh_token_json() -> dict:
    """A canonical token-endpoint response body used across the HTTP tests."""
    return {
        "access_token": "ACCESS-123",
        "refresh_token": "REFRESH-456",
        "token_type": "bearer",
        "expires_in": 7200,
        "scope": None,
    }


def test_authorization_url_has_required_params() -> None:
    """``authorization_url`` must include response_type, client_id, state,
    and redirect_uri - all the parameters hh.ru requires."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler=handler)
    client = HhHttpOAuthClient(
        client_id="CID",
        client_secret="SECRET",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
        transport=transport,
    )

    url = client.authorization_url(
        state="abc123",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "hh.ru"
    assert parsed.path == "/oauth/authorize"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["CID"]
    assert query["state"] == ["abc123"]
    assert query["redirect_uri"] == ["http://localhost:8000/hh/oauth/callback"]


def test_exchange_code_uses_correct_endpoint() -> None:
    """``exchange_code`` must POST to ``/oauth/token`` with
    ``grant_type=authorization_code`` and the supplied code, client
    credentials, and redirect_uri. The response is parsed into
    :class:`HhTokenResponse`."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=_hh_token_json())

    transport = httpx.MockTransport(handler=handler)
    client = HhHttpOAuthClient(
        client_id="CID",
        client_secret="SECRET",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
        transport=transport,
    )

    response = asyncio.run(
        client.exchange_code(
            code="THE-CODE",
            redirect_uri="http://localhost:8000/hh/oauth/callback",
        )
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://hh.ru/oauth/token"
    form = parse_qs(captured["body"])
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == ["CID"]
    assert form["client_secret"] == ["SECRET"]
    assert form["code"] == ["THE-CODE"]
    assert form["redirect_uri"] == ["http://localhost:8000/hh/oauth/callback"]

    assert isinstance(response, HhTokenResponse)
    assert response.access_token == "ACCESS-123"
    assert response.refresh_token == "REFRESH-456"
    assert response.token_type == "bearer"
    assert response.expires_in == 7200


def test_refresh_token_uses_correct_endpoint() -> None:
    """``refresh_tokens`` must POST to ``/oauth/token`` with
    ``grant_type=refresh_token`` and the supplied refresh token."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=_hh_token_json())

    transport = httpx.MockTransport(handler=handler)
    client = HhHttpOAuthClient(
        client_id="CID",
        client_secret="SECRET",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
        transport=transport,
    )

    response = asyncio.run(client.refresh_tokens(refresh_token="OLD-REFRESH"))

    assert captured["method"] == "POST"
    assert captured["url"] == "https://hh.ru/oauth/token"
    form = parse_qs(captured["body"])
    assert form["grant_type"] == ["refresh_token"]
    assert form["client_id"] == ["CID"]
    assert form["client_secret"] == ["SECRET"]
    assert form["refresh_token"] == ["OLD-REFRESH"]
    # ``refresh_tokens`` requests do not include ``code`` or
    # ``redirect_uri`` - hh.ru rejects unexpected parameters on
    # refresh-grant requests.
    assert "code" not in form
    assert "redirect_uri" not in form

    assert response.access_token == "ACCESS-123"


# ---------------------------------------------------------------------------
# HhOAuthStateStore
# ---------------------------------------------------------------------------


def test_state_store_generates_unique_states() -> None:
    """The state store must issue cryptographically distinct states; we
    verify that two consecutive states are not equal and that the
    produced state is reasonably long (URL-safe)."""
    store = HhOAuthStateStore()
    user_id = uuid.uuid4()

    s1 = store.generate_state(user_id=user_id)
    s2 = store.generate_state(user_id=user_id)

    assert s1 != s2
    # 32 bytes -> 43 base64-url chars
    assert len(s1) >= 32
    assert len(s2) >= 32


def test_state_store_validates_once() -> None:
    """States are one-time use: the first ``validate_state`` returns the
    user id, the second returns ``None`` (the state is consumed)."""
    store = HhOAuthStateStore()
    user_id = uuid.uuid4()
    state = store.generate_state(user_id=user_id)

    assert store.validate_state(state) == user_id
    assert store.validate_state(state) is None


def test_state_store_unknown_state_returns_none() -> None:
    """An unknown / never-issued state must yield ``None`` rather than
    raising - that is what the OAuth callback handler will rely on."""
    store = HhOAuthStateStore()
    assert store.validate_state("never-issued") is None


# ---------------------------------------------------------------------------
# HhAuthService - orchestration
# ---------------------------------------------------------------------------


@pytest.fixture
def encryptor() -> CredentialEncryptor:
    return CredentialEncryptor(key=Fernet.generate_key())


@pytest.fixture
def credential_service(encryptor: CredentialEncryptor) -> HHCredentialService:
    return HHCredentialService(
        repo=InMemoryHHCredentialRepository(),
        encryptor=encryptor,
    )


@pytest.fixture
def state_store() -> HhOAuthStateStore:
    return HhOAuthStateStore()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


def test_auth_service_start_authorization(
    state_store: HhOAuthStateStore,
    credential_service: HHCredentialService,
) -> None:
    """``start_authorization`` must return a URL whose ``state`` is
    registered against the user; the URL must point at hh.ru's
    authorize endpoint with the configured client_id and
    redirect_uri."""
    client = InMemoryHhOAuthClient(client_id="CID")
    service = HhAuthService(
        oauth_client=client,
        state_store=state_store,
        credential_service=credential_service,
        client_id="CID",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
    )
    user_id = uuid.uuid4()

    result = service.start_authorization(user_id=user_id)

    assert "authorization_url" in result
    assert "state" in result

    parsed = urlparse(result["authorization_url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "hh.ru"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["CID"]
    assert query["redirect_uri"] == ["http://localhost:8000/hh/oauth/callback"]
    assert query["state"] == [result["state"]]

    # The state is registered and resolvable
    assert state_store.validate_state(result["state"]) == user_id


def test_auth_service_handle_callback_stores_credentials(
    state_store: HhOAuthStateStore,
    credential_service: HHCredentialService,
    user_id: uuid.UUID,
) -> None:
    """``handle_callback`` must validate the state, exchange the
    authorization code for tokens, and store them via the credential
    service. The returned payload reflects the successful connection;
    raw tokens must not leak into the result."""
    oauth = InMemoryHhOAuthClient(
        exchange_responses={
            "AUTH-CODE": HhTokenResponse(
                access_token="NEW-ACCESS",
                refresh_token="NEW-REFRESH",
                token_type="bearer",
                expires_in=3600,
                scope=None,
            )
        }
    )
    state = state_store.generate_state(user_id=user_id)

    service = HhAuthService(
        oauth_client=oauth,
        state_store=state_store,
        credential_service=credential_service,
        client_id="CID",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
    )

    result = asyncio.run(service.handle_callback(code="AUTH-CODE", state=state))

    # The user id round-trips
    assert uuid.UUID(result["user_id"]) == user_id
    # Tokens are persisted - re-read them through the credential service.
    stored = credential_service.get_credentials(user_id)
    assert stored.access_token == "NEW-ACCESS"
    assert stored.refresh_token == "NEW-REFRESH"
    assert stored.token_type == "bearer"
    assert stored.expires_at is not None

    # The state is consumed and cannot be reused
    assert state_store.validate_state(state) is None


def test_auth_service_handle_callback_rejects_bad_state(
    state_store: HhOAuthStateStore,
    credential_service: HHCredentialService,
    user_id: uuid.UUID,
) -> None:
    """A callback with an unknown / consumed state must not exchange
    the code or store any credentials."""
    oauth = InMemoryHhOAuthClient(client_id="CID")
    service = HhAuthService(
        oauth_client=oauth,
        state_store=state_store,
        credential_service=credential_service,
        client_id="CID",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
    )

    with pytest.raises(InvalidOAuthStateError):
        asyncio.run(service.handle_callback(code="ANY", state="never-issued"))

    # No credentials were stored
    with pytest.raises(NotFoundError):
        credential_service.get_credentials(user_id)


def test_auth_service_refresh_token_updates_credentials(
    state_store: HhOAuthStateStore,
    credential_service: HHCredentialService,
    user_id: uuid.UUID,
) -> None:
    """``refresh_user_token`` must read the stored refresh token, call
    the OAuth client with it, and overwrite the stored credentials
    with the new access/refresh tokens."""
    credential_service.store_credentials(
        user_id=user_id,
        access_token="OLD-ACCESS",
        refresh_token="OLD-REFRESH",
        token_type="bearer",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    oauth = InMemoryHhOAuthClient(
        refresh_responses={
            "OLD-REFRESH": HhTokenResponse(
                access_token="REFRESHED-ACCESS",
                refresh_token="REFRESHED-REFRESH",
                token_type="bearer",
                expires_in=7200,
                scope=None,
            )
        }
    )
    service = HhAuthService(
        oauth_client=oauth,
        state_store=state_store,
        credential_service=credential_service,
        client_id="CID",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
    )

    result = asyncio.run(service.refresh_user_token(user_id=user_id))

    assert uuid.UUID(result["user_id"]) == user_id
    stored = credential_service.get_credentials(user_id)
    assert stored.access_token == "REFRESHED-ACCESS"
    assert stored.refresh_token == "REFRESHED-REFRESH"
    # New expiry is roughly now + 7200s
    now = datetime.now(UTC)
    assert stored.expires_at is not None
    delta = (stored.expires_at - now).total_seconds()
    assert 7100 <= delta <= 7200


def test_auth_service_refresh_token_requires_refresh_token(
    state_store: HhOAuthStateStore,
    credential_service: HHCredentialService,
    user_id: uuid.UUID,
) -> None:
    """If the stored credentials do not carry a refresh token, refresh
    must fail (you cannot use the authorization-code grant to mint a
    new access token)."""
    credential_service.store_credentials(
        user_id=user_id,
        access_token="OLD-ACCESS",
        refresh_token=None,
        token_type="bearer",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    oauth = InMemoryHhOAuthClient(client_id="CID")
    service = HhAuthService(
        oauth_client=oauth,
        state_store=state_store,
        credential_service=credential_service,
        client_id="CID",
        redirect_uri="http://localhost:8000/hh/oauth/callback",
    )

    with pytest.raises(MissingRefreshTokenError):
        asyncio.run(service.refresh_user_token(user_id=user_id))
