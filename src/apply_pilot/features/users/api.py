"""FastAPI router for the auth slice.

Endpoints
---------

* ``POST /auth/register`` — create a new user, return the public
  :class:`UserRead` payload. Does NOT log the user in; clients should
  follow up with ``/auth/login``.
* ``POST /auth/login`` — verify credentials and either:
    - return a bearer token + the user payload (JSON client), or
    - redirect to ``/dashboard`` (or the safe ``next`` query) with
      a ``Set-Cookie`` header attached (HTML client).
  Both paths set the browser-friendly session cookie so hybrid
  SPAs can pick whichever credential they prefer.
* ``POST /auth/logout`` — invalidate the supplied bearer token,
  clear the session cookie, and either 204 (JSON) or 303→``/`` (HTML).
* ``GET /auth/login`` — render the inline-HTML login form. A
  visitor with a valid session cookie is bounced to ``?next=...``
  or ``/dashboard`` instead.
* ``GET /auth/me`` — return the user behind a bearer token or the
  session cookie. JSON only.
* ``POST /auth/refresh`` — issue a new bearer token from a still-valid
  existing one (header or cookie).

Wiring
------

The router declares a :func:`get_auth_service` dependency. Production
wiring builds the service with a SQLAlchemy-backed repository, while
tests inject a fake. A request-scoped :func:`get_token_store` is
exposed for the same reason: tokens live in an in-memory store today
but a Redis-backed implementation can drop in later without touching
the route handlers.

Content negotiation
-------------------

The ``/auth/login`` and ``/auth/logout`` endpoints use the
``Accept`` header to choose between the JSON contract (stable for
API consumers) and the HTML contract (stable for the M6 frontend
shell). A single ``text/html`` mention in ``Accept`` flips the
endpoint into HTML mode; everything else stays on the JSON path
that pre-M6 callers depend on.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.audit.service import AuditService, get_audit_service
from apply_pilot.features.telegram.linking import (
    TelegramLinkingService,
    get_linking_service,
)
from apply_pilot.features.users.repository import (
    SqlAlchemyUserSessionRepository,
    SqlAlchemyUsersRepository,
)
from apply_pilot.features.users.schemas import (
    AuthenticatedUser,
    UserCreate,
    UserLogin,
    UserRead,
)
from apply_pilot.features.users.security import (
    InvalidTokenError,
    TokenStore,
    default_token_store,
)
from apply_pilot.features.users.service import (
    AuthenticationError,
    AuthService,
    DuplicateEmailError,
)
from apply_pilot.features.users.session import (
    LOGIN_PATH,
    clear_session_cookie,
    get_session_token,
    set_session_cookie,
)

_LOGGER = logging.getLogger("apply_pilot.features.users.api")

router = APIRouter(prefix="/auth", tags=["auth"])

# ``auto_error=False`` lets us return our own 401 with a stable JSON
# shape instead of FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_token_store() -> TokenStore:
    """Default token store used by the router.

    Returns a process-wide :func:`default_token_store` so tokens
    issued by one request remain resolvable by the next. Production
    wiring can override this dependency to plug in a Redis-backed or
    multi-process store.
    """
    return default_token_store()


def get_auth_service(
    session: Session = Depends(get_db),  # noqa: B008
    tokens: TokenStore = Depends(get_token_store),  # noqa: B008
) -> AuthService:
    """Build an :class:`AuthService` for the current request.

    The service owns user and session repositories backed by the
    request's session. The session itself is closed by the ``get_db``
    generator once the response is sent.
    """
    repo = SqlAlchemyUsersRepository(session=session)
    sessions_repo = SqlAlchemyUserSessionRepository(session=session)
    return AuthService(users_repo=repo, sessions_repo=sessions_repo, tokens=tokens)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _wants_html(request: Request) -> bool:
    """Return ``True`` when the request's Accept header prefers HTML.

    The check is intentionally permissive: any ``text/html`` mention
    in the Accept header flips it on. This matches what every modern
    browser sends and keeps curl users on the JSON path (they
    default to ``*/*``).
    """
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept


def _safe_next(next_value: str | None) -> str:
    """Return a redirect target that is always local to the app.

    Accepts only paths that start with a single ``/`` and are NOT a
    protocol-relative URL (``//evil.example.com/...``). Anything
    else — absolute URLs, scheme-relative, or empty — falls back to
    ``/dashboard`` so a malicious ``next`` value cannot turn the
    login flow into an open redirect.
    """
    if not next_value:
        return "/dashboard"
    parsed = urlparse(next_value)
    # An absolute URL has a ``scheme`` or ``netloc``. Reject.
    if parsed.scheme or parsed.netloc:
        return "/dashboard"
    # Protocol-relative (``//foo``) has ``netloc`` too — already caught.
    if not next_value.startswith("/"):
        return "/dashboard"
    return next_value


def _resolve_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Return the bearer token from the Authorization header OR the cookie.

    Header takes precedence (bearer is the canonical credential);
    the session cookie is a fallback so a browser-based client can
    authenticate without any client-side JavaScript.
    """
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    return get_session_token(request)


def _parse_form_urlencoded(body: bytes) -> dict[str, str]:
    """Parse a ``application/x-www-form-urlencoded`` body.

    We avoid pulling in ``python-multipart`` for a single endpoint:
    the body is a flat ``key=value&key=value`` string, which
    :func:`urllib.parse.parse_qs` handles in two lines. The first
    value of each key wins — the form has no repeated fields, so
    collapsing to a flat ``dict[str, str]`` is exactly what the
    login handler needs.
    """
    text = body.decode("utf-8", errors="replace")
    parsed = parse_qs(text, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


# ---------------------------------------------------------------------------
# Inline HTML templates (M6, issue #169)
# ---------------------------------------------------------------------------
#
# The M6 frontend shell is plain HTML forms — no Jinja2, no SPA
# framework. The style block mirrors the one in
# ``features/admin/api.py:_HEALTH_PAGE_HTML`` and ``app.py:_LANDING_HTML``
# so every page in the project looks like part of one design system.


_LOGIN_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Sign in · ApplyPilot</title>
    <style>
      :root {{
        color-scheme: light dark;
        --bg: #f7f7f8;
        --fg: #1f2328;
        --muted: #57606a;
        --accent: #0969da;
        --card: #ffffff;
        --border: #d0d7de;
        --bad: #cf222e;
      }}
      @media (prefers-color-scheme: dark) {{
        :root {{
          --bg: #0d1117;
          --fg: #e6edf3;
          --muted: #8b949e;
          --accent: #58a6ff;
          --card: #161b22;
          --border: #30363d;
          --bad: #f85149;
        }}
      }}
      body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Oxygen, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
        background: var(--bg);
        color: var(--fg);
        margin: 0;
        padding: 0;
        line-height: 1.5;
      }}
      main {{
        max-width: 420px;
        margin: 0 auto;
        padding: 3rem 1.5rem;
      }}
      header {{
        text-align: center;
        margin-bottom: 2rem;
      }}
      header h1 {{
        font-size: 1.75rem;
        margin: 0 0 0.25rem;
        letter-spacing: -0.02em;
      }}
      header p {{
        color: var(--muted);
        margin: 0;
        font-size: 0.95rem;
      }}
      form {{
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 1.5rem;
        display: grid;
        gap: 1rem;
      }}
      label {{
        display: block;
        font-size: 0.85rem;
        font-weight: 600;
        margin-bottom: 0.25rem;
      }}
      input[type="email"],
      input[type="password"] {{
        width: 100%;
        box-sizing: border-box;
        padding: 0.6rem 0.75rem;
        background: var(--bg);
        color: var(--fg);
        border: 1px solid var(--border);
        border-radius: 6px;
        font-size: 1rem;
        font-family: inherit;
      }}
      input[type="email"]:focus,
      input[type="password"]:focus {{
        outline: 2px solid var(--accent);
        outline-offset: -1px;
        border-color: var(--accent);
      }}
      button {{
        background: var(--accent);
        color: #fff;
        border: 0;
        border-radius: 6px;
        padding: 0.65rem 1rem;
        font-size: 1rem;
        font-weight: 600;
        cursor: pointer;
        font-family: inherit;
      }}
      button:hover {{
        filter: brightness(1.1);
      }}
      p.error {{
        color: var(--bad);
        background: color-mix(in srgb, var(--bad) 10%, transparent);
        border: 1px solid color-mix(in srgb, var(--bad) 35%, transparent);
        border-radius: 6px;
        padding: 0.6rem 0.75rem;
        margin: 0;
        font-size: 0.9rem;
      }}
      footer {{
        text-align: center;
        margin-top: 1.5rem;
        color: var(--muted);
        font-size: 0.85rem;
      }}
      footer a {{
        color: var(--muted);
      }}
    </style>
  </head>
  <body>
    <main>
      <header>
        <h1>Sign in</h1>
        <p>Welcome back. Use your ApplyPilot account.</p>
      </header>
      {error_block}
      <form action="{action}" method="post" autocomplete="on">
        <div>
          <label for="email">Email</label>
          <input id="email" name="email" type="email" required autocomplete="username" />
        </div>
        <div>
          <label for="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            required
            minlength="8"
            autocomplete="current-password"
          />
        </div>
        <input name="next" type="hidden" value="{next_value}" />
        <button type="submit">Sign in</button>
      </form>
      <footer>
        <a href="/">&larr; Back to home</a>
      </footer>
    </main>
  </body>
</html>
"""


def _render_login_form(*, next_value: str, error: str = "") -> str:
    """Render the login form, optionally with an error banner.

    Both the ``next_value`` and ``error`` arguments are HTML-escaped
    before interpolation. The ``action`` attribute is a constant
    (``/auth/login``) so it is not interpolated.
    """
    safe_next = html.escape(next_value, quote=True)
    if error:
        error_block = f'<p class="error" role="alert">{html.escape(error, quote=True)}</p>'
    else:
        error_block = ""
    return _LOGIN_HTML.format(
        action=LOGIN_PATH,
        next_value=safe_next,
        error_block=error_block,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Email already registered"},
        422: {"description": "Validation error"},
    },
)
def register(
    payload: UserCreate,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    audit: AuditService = Depends(get_audit_service),  # noqa: B008
) -> UserRead:
    """Create a new user account."""
    try:
        result = service.register(payload)
        audit.log_event(
            AuditEventType.REGISTER, user_id=result.id, details={"email": payload.email}
        )
        return result
    except DuplicateEmailError as exc:
        _LOGGER.info("auth.register.conflict", extra={"email": payload.email})
        raise _http_error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc


@router.get(
    "/login",
    response_class=HTMLResponse,
    include_in_schema=True,
    responses={
        200: {"description": "Render the login form"},
        303: {"description": "Already authenticated; redirect to next or /dashboard"},
    },
    summary="Render the login form (HTML)",
)
def login_form(
    request: Request,
    next: str | None = None,  # noqa: A002 - matches the public ``?next=`` query name
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> Response:
    """Render the inline-HTML login form (M6, issue #169).

    If the caller already has a valid session cookie, they are
    bounced to ``?next=...`` (when safe) or ``/dashboard`` instead
    of being shown the form.
    """
    cookie_token = get_session_token(request)
    if cookie_token:
        try:
            service.resolve_user_id_from_token(cookie_token)
            # Cookie resolves to a real user — bounce.
            target = _safe_next(next)
            return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
        except InvalidTokenError:
            # Stale / revoked cookie — fall through to the form.
            pass
    return HTMLResponse(
        content=_render_login_form(next_value=next or "", error=""),
    )


@router.post(
    "/login",
    response_model=AuthenticatedUser,
    responses={
        303: {"description": "HTML form: 303 redirect with Set-Cookie"},
        401: {"description": "Invalid credentials"},
        422: {"description": "Validation error"},
    },
)
async def login(
    request: Request,
    response: Response,
    next: str | None = None,  # noqa: A002 - public ``next`` query/param name
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    audit: AuditService = Depends(get_audit_service),  # noqa: B008
) -> Response:
    """Verify credentials and return a bearer token + user payload (M6).

    The endpoint is content-negotiated via the ``Accept`` header:

    * ``Accept: text/html`` — on success, set the session cookie and
      303-redirect to the safe ``next`` value (or ``/dashboard``);
      on failure, re-render the login form with an error message.
    * Otherwise (the JSON default) — return the bearer token JSON,
      *and* set the session cookie so hybrid clients can use either
      credential.

    The ``next`` field is accepted as a query parameter (the JSON
    path) or as a form field (the HTML path). FastAPI's single
    ``next: str | None = None`` declaration covers both because
    form fields and query parameters share the same namespace for
    a given route.
    """
    # We avoid pulling in ``python-multipart`` for a single endpoint.
    # The form-encoded body is a flat ``key=value&key=value`` string
    # that ``urllib.parse.parse_qs`` handles in two lines; the JSON
    # path is parsed by ``request.json()``.
    content_type = request.headers.get("content-type", "").lower()
    email: str | None = None
    password: str | None = None
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as exc:
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                f"invalid JSON body: {exc}",
            ) from exc
        if not isinstance(body, dict):
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "JSON body must be an object",
            )
        try:
            payload = UserLogin.model_validate(body)
        except Exception as exc:
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                str(exc),
            ) from exc
        email = payload.email
        password = payload.password
    elif "application/x-www-form-urlencoded" in content_type:
        raw = await request.body()
        fields = _parse_form_urlencoded(raw)
        email = fields.get("email")
        password = fields.get("password")
        # The HTML form posts ``next`` as a hidden input. Prefer the
        # form value over the query string so the explicit user-visible
        # redirect target wins.
        form_next = fields.get("next")
        if form_next is not None:
            next = form_next
    else:
        raise _http_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "Content-Type must be application/json or application/x-www-form-urlencoded",
        )

    if not email or not password:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "email and password are required",
        )

    try:
        result = service.login(email=email, password=password)
    except AuthenticationError as exc:
        # 401 is the same code for unknown email, wrong password, or
        # inactive user; the response body never reveals which.
        _LOGGER.info("auth.login.failed", extra={"email": email})
        if _wants_html(request):
            return HTMLResponse(
                content=_render_login_form(
                    next_value=next or "",
                    error="Invalid email or password.",
                ),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED, exc.code, "invalid email or password"
        ) from exc

    audit.log_event(AuditEventType.LOGIN, user_id=result.user.id, details={"email": email})

    # Always set the session cookie so hybrid clients can use either
    # the bearer token or the cookie. The cookie is bound to the same
    # raw token that the JSON body returns, so
    # :meth:`AuthService.resolve_user_id_from_token` resolves both.
    set_session_cookie(response, token=result.access_token)

    if _wants_html(request):
        target = _safe_next(next)
        return RedirectResponse(
            url=target,
            status_code=status.HTTP_303_SEE_OTHER,
            headers=response.headers,
        )

    # JSON path: build a fresh Response with the model body. The
    # ``response`` parameter is the one whose ``Set-Cookie`` we just
    # populated, so its headers (incl. the cookie) carry over.
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content=jsonable_encoder(result),
        headers=response.headers,
        status_code=status.HTTP_200_OK,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        303: {"description": "HTML form: 303 redirect to /"},
        401: {"description": "Missing or invalid bearer token"},
    },
)
def logout(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    tokens: TokenStore = Depends(get_token_store),  # noqa: B008
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> Response:
    """Invalidate the supplied bearer token (header or cookie).

    Idempotent: a missing or already-revoked token still returns 204
    on the JSON path when at least one credential is present (and
    401 when neither is). The HTML path always bounces to ``/`` so
    a user clicking a logout link never sees an error page.
    """
    token = _resolve_bearer_token(request, credentials)
    if token is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    service.logout(token)
    clear_session_cookie(response)
    if _wants_html(request):
        # Carry the cookie-clearing ``Set-Cookie`` header from the
        # response object onto the redirect so the browser drops the
        # session cookie on its way to ``/``.
        return RedirectResponse(
            url="/",
            status_code=status.HTTP_303_SEE_OTHER,
            headers=response.headers,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=response.headers)


@router.post(
    "/refresh",
    response_model=AuthenticatedUser,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def refresh(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> AuthenticatedUser:
    """Issue a new bearer token from a still-valid existing token.

    The old token is invalidated and a fresh session is created.
    The session cookie is also rotated to the new token so a
    browser-based client does not have to manually re-issue the
    cookie.
    """
    token = _resolve_bearer_token(request, credentials)
    if token is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    try:
        new = service.refresh_token(token)
    except AuthenticationError as exc:
        _LOGGER.info("auth.refresh.failed")
        raise _http_error(status.HTTP_401_UNAUTHORIZED, exc.code, "invalid token") from exc
    set_session_cookie(response, token=new.access_token)
    return new


@router.get(
    "/me",
    response_model=UserRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def me(
    request: Request,
    response: Response,  # noqa: ARG001 - reserved for future use
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> UserRead:
    """Return the user behind the bearer token (header or cookie)."""
    token = _resolve_bearer_token(request, credentials)
    if token is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    try:
        user_id = service.resolve_user_id_from_token(token)
    except InvalidTokenError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc)) from exc
    try:
        return service.get_user(user_id=user_id)
    except AuthenticationError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc)) from exc


@router.get(
    "/telegram-link",
    responses={
        200: {"description": "Linking code generated"},
        401: {"description": "Missing or invalid bearer token"},
    },
)
def telegram_link(
    request: Request,
    response: Response,  # noqa: ARG001 - reserved for future use
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    linking: TelegramLinkingService = Depends(get_linking_service),  # noqa: B008
) -> dict[str, str]:
    """Generate a one-time Telegram linking code for the authenticated user."""
    token = _resolve_bearer_token(request, credentials)
    if token is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    try:
        user_id = service.resolve_user_id_from_token(token)
    except InvalidTokenError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc)) from exc

    code = linking.generate_token(user_id=str(user_id))
    return {"linking_code": code}


# ---------------------------------------------------------------------------
# Body helpers (intentionally tiny and local to this module)
# ---------------------------------------------------------------------------
#
# We deliberately avoid pulling in ``python-multipart`` for a single
# endpoint. The form-encoded body is a flat ``key=value&key=value``
# string, which :func:`urllib.parse.parse_qs` handles in two lines.
# The JSON body is read with ``await request.json()`` (the canonical
# FastAPI pattern inside an ``async def`` handler).


__all__ = [
    "get_auth_service",
    "get_token_store",
    "login_form",
    "router",
]
