# docs/integrations/hh_apply.md

## Discovery plan: hh.ru negotiation endpoint integration for apply-pilot

> Source of truth for T2 (#243) implementation. Refs: M11 epic [#239](https://github.com/q-user/apply-pilot/issues/239), this issue [#242](https://github.com/q-user/apply-pilot/issues/242). **Do not copy code from `/home/mikhail/projects/hh_apply`** — only read-only shape orientation per #239 comments #2 + #4 (no `hh-applicant-tool` dep, no vendor, no fork; mechanism = User-Agent + XSRF + headers, **no Selenium / playwright**).

**Reconcile date**: 2026-06-26 (against `/home/mikhail/projects/hh_apply` snapshot — `job_bot` namespace, post 2.0.0 cleanup of `hh_applicant_tool`).

## Goal

Add hh.ru apply capability to apply-pilot as a vertical slice under `src/apply_pilot/features/hh_apply/`. The mechanism dispels the common "HH API закрыт для ботов" belief: HH's official `negotiation` endpoint is **the same endpoint** the public-facing site uses, but the bot needs to imitate the **Android mobile app's** request fingerprint (User-Agent + cookies + headers), not a desktop browser. **No Selenium, no playwright, no third-party apply-package.**

## Constraints

- ❌ **Selenium / playwright / pywebview** — never used in code or dependencies.
- ❌ **`hh-applicant-tool` or any production-ready apply-package** — never added as dependency; no `pip install`, no vendor, no fork, no subtree.
- ✅ Our own code; async client; DI-friendly; surface-limited dependencies (`httpx` + `pydantic`).
- ✅ Read-only orientation against `/home/mikhail/projects/hh_apply` (this file's scope).

---

## 1. Endpoint discovery

**Primary target**: `POST https://hh.ru/negotiations` (JSON body).

**Secondary target**: `GET /negotiations/{id}/messages` (read negotiation state — used by T5 idempotency check).

**Optional modernization**: `POST /applicant/v2/negotiations` may exist on newer Android clients. T2 implementation must default to `/negotiations` (proven by hh_apply) and fall back to `/applicant/v2/...` only on a configurable endpoint if HTTP shape changes.

### Request body fields (POST `/negotiations`)

| Field    | Type | Required | Source                                | Notes                                                                                              |
| -------- | ---- | -------- | ------------------------------------- | -------------------------------------------------------------------------------------------------- |
| vacancy_id | str  | yes      | `vacancy["id"]` from `GET /vacancies/{id}` | Already collected by apply-pilot's vacancy_search slice.                                            |
| resume_id  | str  | yes      | config (`HHApplySettings` per T3)     | Per-resume credential; isolated via per-tenant override later (T6).                                  |
| message    | str  | yes      | cover_letter renderer output          | Empty → 4xx `message required`.                                                                     |
| lux        | bool | no       | config gating                         | Premium-apply flag for paid accounts; default `false` in OSS single-user mode.                      |
| force      | bool | no       | retry-only, T5 idempotency            | Set on idempotency re-run path; never `True` on first call.                                          |

### Response codes → ApplyResult.status

| Status   | Meaning                          | ApplyResult.status         |
| -------- | -------------------------------- | -------------------------- |
| 200 / 201| success                          | `success`                  |
| 400      | validation error                 | `validation_error`         |
| 401      | `_xsrf` mismatch / csrf_invalid  | `auth_required` (auto-refresh XSRF + retry ONCE, then `auth_required`) |
| 409      | already_applied                  | `idle_already_applied`     |
| 429      | rate-limit                       | `rate_limited` (backoff + retry) |
| 5xx      | upstream error                   | `upstream_error` (retry with exponential backoff) |

### Result DTO shape (preview, T2 implements)

```python
class ApplyStatus(str, Enum):
    success = "success"
    idle_already_applied = "idle_already_applied"
    validation_error = "validation_error"
    auth_required = "auth_required"
    rate_limited = "rate_limited"
    upstream_error = "upstream_error"

class ApplyResult(BaseModel):
    status: ApplyStatus
    negotiation_id: str | None = None
    http_status: int
    raw: dict[str, Any] | None = None  # full response body for diagnostics
    attempt_count: int = 1
```

---

## 2. Cookie + XSRF handshake

**Mechanism**: HH.ru's `negotiation` endpoint enforces Django's standard XSRF/Cookie duplex. The client must:

1. **Establish a session** by GET'ing `https://hh.ru/` once. Server responds with `Set-Cookie: _xsrf=...; HttpOnly; SameSite=Lax`. The `_xsrf` value is the **session-bound XSRF token**, mirrored verbatim from the cookie jar into the next POST's `X-XSRF-Token` header.

2. **Send XSRF back on each POST** as `X-XSRF-Token: <value>` (or `X-CSRFToken` for older Django). The value is **always** the current `_xsrf` cookie content.

3. **Filter non-HH cookies from the jar** — only persist cookies matching `*.hh.ru`, `hh.kz`, `hh.uz`. T2 client subclasses `httpx.AsyncClient` and overrides `_init_cookie_jar` with a filtering `MozillaCookieJar` (pattern inspired by hh_apply's `HHOnlyCookieJar`, **re-implemented, not copied**).

### Refresh semantics

- `_xsrf` TTL: server-session-scoped (browser dies → token dies). Server may re-issue on demand.
- If response is 401 with `csrf_invalid` → trigger `fetch_xsrf_token()` (single GET to root) and retry the **same** request **ONCE** with fresh `_xsrf`.
- Otherwise, do **not** re-fetch — saves one round-trip per apply.

### Implementation hooks (T2 contracts)

- `HHApplyClient.fetch_xsrf_token() -> str` — bootstrap or refresh.
- `HHApplyClient.request_with_xsrf_retry(...)` — wraps `client.post(...)` with auto-refresh on `csrf_invalid`.
- Cookie jar constructor accepts an allowlist of domains, default `{"hh.ru", "hh.kz", "hh.uz"}`.

---

## 3. Required headers

For T1 purposes (T2 implements). Tokens marked `[CONTROLLED]` are bound to per-tenant isolation in T6.

| Header           | Example / pattern                                       | Control                                                 | Why                                                                                          |
| ---------------- | ------------------------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `User-Agent`     | `ru.hh.android/<version> (Android; <os>; <device>)`     | `[CONTROLLED]` per T3 `HHApplySettings.user_agent`      | HH traffic-classifies by UA. Android emulation gates us into the mobile apply flow.            |
| `Accept`         | `application/json, text/plain, */*`                     | static                                                  | Match Android client.                                                                        |
| `Accept-Language`| `ru-RU,ru;q=0.9,en;q=0.8`                               | static                                                  | Russian locale required for proper vacancy matching.                                          |
| `Accept-Encoding`| `gzip, deflate, br`                                     | static                                                  | Standard.                                                                                    |
| `Referer`        | `https://hh.ru/`                                        | static                                                  | Required or 403.                                                                             |
| `Origin`         | `https://hh.ru`                                         | static                                                  | Same; required for `SameSite` enforcement.                                                   |
| `X-XSRF-Token`   | `<value from _xsrf cookie>`                             | dynamic per request                                     | See §2.                                                                                       |
| `Cookie`         | (httpx-managed from allowlist-only jar)                 | dynamic per request                                     | Includes `_xsrf`, `hhuid`, etc. Non-`hh.*` cookies dropped.                                    |
| `Content-Type`   | `application/json` (or `application/x-www-form-urlencoded` on form-fallback) | static                              | Match Android REST contract.                                                                  |
| `X-Requested-With`| `XMLHttpRequest`                                       | static                                                  | Mark as AJAX apply.                                                                          |

### Defaults locked at T1

- `user_agent` default placeholder: `ru.hh.android/<stable> (Android; 14; Pixel 7)`. **T2 must pick a concrete version string** before merge — sourced from currently-deployed Android app metadata (publicly available via Play Store / APK metadata), **NOT** reverse-engineered from hh_apply.
- All other headers static, no env overrides required (Russian locale is mandatory for HH.ru region).

> **Per #239 comment #4**: the Android UA is **public** — HH publishes it. We do not extract it by reverse-engineering hh_apply; we pick a stable, conservative default.

---

## 4. Request / response shapes

### 4.1 POST `/negotiations` (JSON, default)

```json
{
  "vacancy_id": "12345678",
  "resume_id": "abc-resume-token",
  "message": "Здравствуйте, ...",
  "lux": false,
  "force": false
}
```

Alternative: form-urlencoded with same fields. **T2 default = JSON**; fall back to form-urlencoded only on retry-after-400-error where server returns `Content-Type: application/x-www-form-urlencoded` requirement.

### 4.2 200 OK response

```json
{
  "id": "1234567890",
  "vacancy_id": "12345678",
  "resume_id": "abc-resume-token",
  "state": "applied",
  "created_at": "2026-06-26T22:00:00+03:00",
  "updated_at": "2026-06-26T22:00:00+03:00"
}
```

### 4.3 Error responses (canonical shapes; only JSON examples shown — T2 must accept whatever encoding the server sends)

```json
// 400 (validation)
{ "errors": { "message": ["Required field"], "vacancy_id": ["Not found"] } }

// 401 (csrf)
{ "error": "csrf_invalid", "_xsrf_mismatch": true }

// 409 (already applied)
{ "error": "already_applied", "negotiation_id": "1234567890" }

// 429 (rate-limit)
{ "errors": { "__all__": ["Too many requests"] } }

// 5xx
{ "error": "upstream", "request_id": "..." }
```

### 4.4 Result DTO (apply-pilot, T2 preview — DO NOT IMPLEMENT in T1)

```python
# src/apply_pilot/features/hh_apply/models.py (T2 implements)

class ApplyRequest(BaseModel):
    vacancy_id: str
    resume_id: str
    message: str
    lux: bool = False
    force: bool = False  # T5 (worker integration) idempotency override

class ApplyError(BaseModel):
    code: str
    message: str
    http_status: int
    raw: dict[str, Any] | None = None

class ApplyResult(BaseModel):
    status: ApplyStatus  # enum from §1 table
    negotiation_id: str | None = None
    http_status: int
    raw: dict[str, Any] | None = None
    attempt_count: int = 1
    error: ApplyError | None = None
```

---

## 5. Minimal Python code-shape proposal

This is the artifact **T2 (#243) implements**. **Not code yet** — design only.

### File layout

```
src/apply_pilot/features/hh_apply/
├── __init__.py           # re-exports: apply_once, ApplyRequest, ApplyResult,
│                         #           ApplyError, ApplyStatus
├── models.py             # ApplyRequest, ApplyResult, ApplyError, ApplyStatus (Pydantic)
├── client.py             # HHApplyClient (async httpx.AsyncClient subclass)
│                         #   - allowlist-only cookie jar (hh.* domains)
│                         #   - header layer: User-Agent / Origin / Referer /
│                         #     Accept-Language / X-Requested-With
│                         #   - fetch_xsrf_token() bootstrap
│                         #   - request_with_xsrf_retry() wrapper
│                         #     (auto-refresh on csrf_invalid)
├── service.py            # apply_once(request, *, client, retry_policy)
│                         #           -> ApplyResult
│                         #   - error mapping (HH error JSON -> ApplyStatus enum)
│                         #   - structured logging of attempts (consumed by T6)
│                         #   - idempotency key (vacancy_id, resume_id,
│                         #     cover_letter_hash) per T5 dispatch
├── config.py             # Pydantic Settings HHApplySettings (T3 fully implements)
└── tenancy.py            # T6-stub: TenantCredentialProvider Protocol +
                          #           EnvTenantCredentialProvider
```

```
tests/features/hh_apply/             # see T4 #245
├── conftest.py                       # shared fakes: InMemoryCookieJar,
│                                     #                 FakeClock, FakeTransport
├── test_models.py                    # ApplyRequest / ApplyResult /
│                                     #   ApplyError invariants
├── test_client.py                    # X-XSRF-Token fetch + refresh-on-stale
├── test_service.py                   # apply_once:
│                                     #   happy / 4xx / 5xx / XSRF-stale-then-retry
├── test_config.py                    # HHApplySettings env-loading +
│                                     #   per-tenant override
└── test_optional_live.py             # @pytest.mark.optional_local_only —
                                      # skipped in CI, run manually
```

### Service signature (frozen contract for T2)

```python
async def apply_once(
    request: ApplyRequest,
    *,
    client: HHApplyClient,
    retry_policy: RetryPolicy | None = None,
) -> ApplyResult:
    """Submit a single apply to hh.ru.

    Raises:
        HHApplyError: unrecoverable (e.g. auth_required with no retry path).
    """
```

### Retry policy (T3 configures; T6 instruments)

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    request_delay_ms: int = 750
    backoff_multiplier: float = 2.0
    jitter_ms: int = 200
```

### DI pattern (mirrors apply-pilot's existing `apply_worker` slice — `ApplyWorkerProcess` / `ApplyJobService`)

- `HHApplyClient` instance created **once** at worker startup, injected into `apply_once`.
- `RetryPolicy` constructed from `HHApplySettings` (T3) **once** at worker startup, injected.
- `TenantCredentialProvider` from T6 — same DI endpoint, injected.
- Tests use `InMemoryCookieJar` + `FakeTransport` (`httpx.MockTransport` subclass); **no real hh.ru in CI**.

### No new dependencies

Per T2 acceptance criteria and apply-pilot's existing `pyproject.toml`:

| Already-in (≥)                                  | Used for                                  | New?  |
| ----------------------------------------------- | ----------------------------------------- | ----- |
| `httpx>=0.28.1`                                 | async client + MockTransport for tests    | ❌ no |
| `pydantic>=2.9`                                 | DTOs + Settings                           | ❌ no |
| `pytest-asyncio>=1.4.0` (asyncio_mode = "auto") | async test runner                         | ❌ no |
| `pytest>=8.3.2`                                 | test framework                            | ❌ no |
| `httpx` `MockTransport` (built-in)              | fake transport for tests                  | ❌ no |

If T4 (#245) prefers a friendlier test double for HTTP (e.g. `respx` for declarative mocking), file this in T4 AC as a **permitted exception** — do not silently introduce in T2.

---

## Acceptance criteria cross-reference (T1 #242)

| #242 AC                                                | This doc covers                                                                |
| ------------------------------------------------------ | ------------------------------------------------------------------------------ |
| Section 1 — Endpoint discovery                         | §1                                                                             |
| Section 2 — Cookie + XSRF handshake                    | §2 (with refresh-on-401 semantics + allowlist-only jar pattern)                |
| Section 3 — Required headers                            | §3 (full header table with control tags)                                       |
| Section 4 — req/resp shapes                            | §4 with status codes + DTO preview                                             |
| Section 5 — Minimal Python code-shape                  | §5 (file layout + service signature + retry policy + DI pattern + no-new-deps)|
| Header includes reconcile date                          | top of file ✓                                                                  |
| Links to [#239](https://github.com/q-user/apply-pilot/issues/239) + related [#206](https://github.com/q-user/apply-pilot/issues/206) | top + §5 (close-out dependent)                                                 |
| No vendored code from `/home/mikhail/projects/hh_apply` | only structural/orientation references — no verbatim code                      |
| PR closes this issue via `Closes #<T1-number>`         | PR body uses `Closes #242`                                                    |

## References

- M11 epic: [#239](https://github.com/q-user/apply-pilot/issues/239) (with comments #1, #2, #3, #4, #5, #6 [batch proposal + close-out note])
- T1 issue: [#242](https://github.com/q-user/apply-pilot/issues/242) (this doc closes it)
- Consumer slices:
  - T2 [#243](https://github.com/q-user/apply-pilot/issues/243) — adapter implementation
  - T3 [#244](https://github.com/q-user/apply-pilot/issues/244) — config layer
  - T4 [#245](https://github.com/q-user/apply-pilot/issues/245) — VSA test slice
  - T5 [#246](https://github.com/q-user/apply-pilot/issues/246) — worker integration, **closes [#206](https://github.com/q-user/apply-pilot/issues/206)**
  - T6 [#247](https://github.com/q-user/apply-pilot/issues/247) — observability + SaaS-readiness
- External read-only orientation: `/home/mikhail/projects/hh_apply` (observed patterns only — no code copy)
