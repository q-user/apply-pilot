# HH OAuth2 Authorization-Code Flow

Issue: #19 (M2 — hh account connection)

## Goals

* Implement the OAuth2 authorization-code grant against hh.ru.
* Persist the resulting tokens through the existing `HHCredentialService`
  so M1's encryption-at-rest story stays intact.
* Expose three HTTP endpoints that match what a frontend would need:

  * `GET /hh/oauth/authorize` — start the flow, return
    `{authorization_url, state}`.
  * `GET /hh/oauth/callback?code=&state=` — public endpoint hh.ru
    redirects the user's browser back to. Validates the state,
    exchanges the code, stores the tokens, returns redacted metadata.
  * `POST /hh/oauth/refresh` — exchange the stored refresh token for a
    fresh access token.

## VSA boundaries

All new code lives in `src/apply_pilot/features/hh/`. The slice is wired
together with constructor-injected dependencies, so the OAuth client
and the state store are swappable for fakes in tests.

| Module                          | Responsibility                                           |
| ------------------------------- | -------------------------------------------------------- |
| `oauth.py`                      | Protocol, value object, state store, orchestrating service |
| `encryption.py` (existing)      | Fernet encryptor (unchanged)                             |
| `repository.py` (existing)      | SQL/in-memory credential store (unchanged)               |
| `service.py` (existing)         | `HHCredentialService` (unchanged)                        |
| `api.py` (extended)             | Three new route handlers + dependency providers          |

`HHCredentialService` already exposes the storage / encryption contract
that the OAuth slice needs. The OAuth slice does not introduce a new
storage layer; it composes `HHCredentialService` with the OAuth client
and the state store.

## Flow

```
client                 apply-pilot API               hh.ru
  |                         |                          |
  |-- GET /hh/oauth/authorize (Bearer) ------------>   |
  |<-- 200 {authorization_url, state} ----------|       |
  |                         |                          |
  |  -- browser redirect --|------------------------> |
  |                         |                          |
  |  <-- GET /hh/oauth/callback?code=...&state=... -|
  |                         |                          |
  |                         |--- POST /oauth/token --> |
  |                         |<-- {access_token,...} ---|
  |<-- 200 redacted creds --|                          |
  |                         |                          |
  |-- POST /hh/oauth/refresh (Bearer) ------------>    |
  |                         |--- POST /oauth/token --> |
  |                         |<-- {access_token,...} ---|
  |<-- 200 redacted creds --|                          |
```

## State

A short-lived, in-memory state store maps `state -> user_id` and
consumes the state on first use (`secrets.token_urlsafe(32)` for
generation, `threading.Lock` to guard the dict). A Redis-backed
implementation is out of scope for this slice but the contract is
narrow enough that swapping it in later is a 1-file change.

## Configuration

`HhOAuthSettings` (in `src/apply_pilot/config.py`) reads:

* `APP_HH_CLIENT_ID` — required
* `APP_HH_CLIENT_SECRET` — required
* `APP_HH_REDIRECT_URI` — optional, default
  `http://localhost:8000/hh/oauth/callback`

The existing `APP_HH_ENCRYPTION_KEY` (Fernet key) is unchanged.

## SemVer impact

Minor bump (additive new feature; no breaking changes to existing
`/hh/credentials` endpoints or the credential model). The package
version moves from `0.4.0` to `0.5.0`.

## Error mapping

| Error class                  | HTTP status |
| ---------------------------- | ----------- |
| `InvalidOAuthStateError`     | 400         |
| `OAuthExchangeError`         | 502         |
| `NotFoundError` (no creds)   | 404         |
| `MissingRefreshTokenError`   | 400         |

`OAuthExchangeError` is mapped to 502 because the failure is on the
upstream hh.ru side, not in our service.

## Tests

* `tests/features/hh/test_oauth.py` — service-level tests with
  `InMemoryHhOAuthClient`, `InMemoryHHCredentialRepository`, and a real
  Fernet encryptor. The `HhHttpOAuthClient` is exercised through
  `httpx.MockTransport` so no real network traffic is generated.
* `tests/features/hh/test_oauth_api.py` — FastAPI integration tests
  that override the OAuth dependencies to inject fakes.
