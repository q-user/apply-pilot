"""HTML surface for the admin slice (M6, issue #171).

The admin slice grows three operator-facing HTML pages:

* ``GET /admin/``              — landing page with nav links to every
  admin subpage and a sign-out form.
* ``GET /admin/integrations``  — read-only HTML table of every
  :class:`IntegrationStatus` snapshot.
* ``GET /admin/users``         — paginated HTML table of every
  registered user with their ``is_active`` / ``is_admin`` flags.

All three pages require a valid bearer token (header) or session
cookie (PR #170) that resolves to a user with ``is_admin=True``.
Missing or anonymous callers are redirected to the login page;
authenticated-but-non-admin callers receive a ``403`` HTML page with
a clear explanation and a link back to ``/``.

The HTML is inline — no Jinja2, no SPA framework — matching the
style of :data:`app._LANDING_HTML`,
:data:`features.admin.api._HEALTH_PAGE_HTML`, and
:data:`features.users.api._LOGIN_HTML`. Every interpolated value is
HTML-escaped via :func:`html.escape` so a user-supplied email like
``weird<x>@example.io`` cannot inject markup into the page.
"""

from __future__ import annotations

import html
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.admin._auth import _bearer_scheme, _resolve_bearer_token
from apply_pilot.features.admin.api import get_integration_status_store
from apply_pilot.features.admin.integrations import IntegrationStatusStore
from apply_pilot.features.admin.schemas import integration_status_to_read
from apply_pilot.features.users.models import User
from apply_pilot.features.users.repository import SqlAlchemyUsersRepository
from apply_pilot.features.users.security import InvalidTokenError, default_token_store
from apply_pilot.features.users.session import LOGIN_PATH

_LOGGER = logging.getLogger("apply_pilot.features.admin.web")

router = APIRouter(prefix="/admin", tags=["admin-web"])


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------


# Re-use the same dependency the JSON admin routes depend on. A
# single override in tests covers both the JSON and HTML surfaces.


# ---------------------------------------------------------------------------
# Page templates
# ---------------------------------------------------------------------------
#
# The CSS block mirrors :data:`features.admin.api._HEALTH_PAGE_HTML` and
# :data:`app._LANDING_HTML` so every page in the project looks like
# part of one design system. Light/dark mode tokens are kept in
# :root and ``@media (prefers-color-scheme: dark)``.

_BADGE_CLASS_BY_VALUE: dict[bool, str] = {
    True: "badge badge-on",
    False: "badge badge-off",
}


_BASE_CSS = """\
:root {{
  color-scheme: light dark;
  --bg: #f7f7f8;
  --fg: #1f2328;
  --muted: #57606a;
  --accent: #0969da;
  --card: #ffffff;
  --border: #d0d7de;
  --ok: #1a7f37;
  --warn: #9a6700;
  --bad: #cf222e;
  --neutral: #57606a;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --card: #161b22;
    --border: #30363d;
    --ok: #3fb950;
    --warn: #d29922;
    --bad: #f85149;
    --neutral: #8b949e;
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
  max-width: 960px;
  margin: 0 auto;
  padding: 3rem 1.5rem;
}}
header.page {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 1.5rem;
  flex-wrap: wrap;
  gap: 1rem;
}}
header.page h1 {{
  font-size: 2rem;
  margin: 0;
  letter-spacing: -0.02em;
}}
header.page .identity {{
  display: flex;
  align-items: center;
  gap: 1rem;
  color: var(--muted);
  font-size: 0.9rem;
}}
nav.admin-nav {{
  display: flex;
  gap: 1rem;
  margin: 0 0 2rem;
  padding: 0;
  flex-wrap: wrap;
}}
nav.admin-nav a {{
  color: var(--accent);
  text-decoration: none;
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  font-size: 0.9rem;
}}
nav.admin-nav a:hover {{
  border-color: var(--accent);
}}
form.inline {{
  display: inline;
  margin: 0;
}}
button.signout {{
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.4rem 0.75rem;
  font-size: 0.85rem;
  cursor: pointer;
  font-family: inherit;
}}
button.signout:hover {{
  color: var(--bad);
  border-color: var(--bad);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}}
th, td {{
  text-align: left;
  padding: 0.6rem 0.85rem;
  border-bottom: 1px solid var(--border);
  font-size: 0.92rem;
  vertical-align: top;
}}
th {{
  background: var(--bg);
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 0.75rem;
}}
tr:last-child td {{
  border-bottom: 0;
}}
.badge {{
  display: inline-block;
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 600;
}}
.badge.badge-on {{
  color: var(--ok);
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent);
}}
.badge.badge-off {{
  color: var(--muted);
  background: var(--bg);
  border: 1px solid var(--border);
}}
.status.healthy {{ color: var(--ok); }}
.status.degraded {{ color: var(--warn); }}
.status.unhealthy {{ color: var(--bad); }}
.status.unknown {{ color: var(--muted); }}
pre.metadata {{
  margin: 0;
  font-size: 0.78rem;
  background: var(--bg);
  padding: 0.4rem 0.5rem;
  border-radius: 4px;
  overflow-x: auto;
  max-width: 360px;
}}
footer.pagination {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 1.5rem;
  color: var(--muted);
  font-size: 0.9rem;
}}
footer.pagination a {{
  color: var(--accent);
  text-decoration: none;
  padding: 0.4rem 0.85rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
}}
footer.pagination a:hover {{
  border-color: var(--accent);
}}
footer.pagination a.disabled {{
  color: var(--muted);
  pointer-events: none;
  opacity: 0.5;
}}
.error-banner {{
  background: color-mix(in srgb, var(--bad) 10%, transparent);
  color: var(--bad);
  border: 1px solid color-mix(in srgb, var(--bad) 35%, transparent);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin: 0 0 1.5rem;
}}
"""


def _format_iso(dt: Any) -> str:
    """Render a datetime as an ISO-8601 string, or ``"-"`` when ``None``."""
    if dt is None:
        return "-"
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _format_metadata(meta: Any) -> str:
    """Render the integration ``metadata`` as a JSON-prettified string."""
    if meta is None:
        return ""
    try:
        return json.dumps(dict(meta), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(meta)


def _build_status_class(status_value: str) -> str:
    """Map an integration status to a CSS class for the row label."""
    allowed = {"healthy", "degraded", "unhealthy", "unknown"}
    if status_value in allowed:
        return f"status {status_value}"
    return "status unknown"


def _build_nav(active: str) -> str:
    """Render the admin subpage nav, marking the *active* page."""
    items = [
        ("health", "/admin/health"),
        ("integrations", "/admin/integrations"),
        ("users", "/admin/users"),
    ]
    rendered: list[str] = []
    for label, href in items:
        marker = " (current)" if label == active else ""
        rendered.append(f'<a href="{html.escape(href)}">{html.escape(label)}{marker}</a>')
    return "\n      ".join(rendered)


def _build_identity(user: User) -> str:
    """Render the ``Logged in as <email>`` line + sign-out form."""
    safe_email = html.escape(user.email)
    return (
        f"<span>Logged in as <strong>{safe_email}</strong></span>"
        f'<form class="inline" method="post" action="/auth/logout">'
        f'<button class="signout" type="submit">Sign out</button>'
        f"</form>"
    )


def _build_pagination(
    *,
    page: int,
    size: int,
    total: int,
    path: str,
) -> str:
    """Render the prev/next footer for the paginated users page."""
    total_pages = max(1, (total + size - 1) // size) if total else 1
    has_prev = page > 1
    has_next = page < total_pages
    prev_href = f"{path}?page={page - 1}&size={size}" if has_prev else ""
    next_href = f"{path}?page={page + 1}&size={size}" if has_next else ""
    prev_link = (
        f'<a href="{html.escape(prev_href)}">&larr; Prev</a>'
        if has_prev
        else '<span class="disabled">&larr; Prev</span>'
    )
    next_link = (
        f'<a href="{html.escape(next_href)}">Next &rarr;</a>'
        if has_next
        else '<span class="disabled">Next &rarr;</span>'
    )
    return (
        f'<footer class="pagination">'
        f"{prev_link}"
        f"<span>Page {page} of {total_pages} &middot; {total} total</span>"
        f"{next_link}"
        f"</footer>"
    )


# ---------------------------------------------------------------------------
# Templates (string constants used with ``.format(**kwargs)``)
# ---------------------------------------------------------------------------


_ADMIN_LANDING_HTML = (
    """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin · ApplyPilot</title>
    <style>
"""
    + _BASE_CSS
    + """
    </style>
  </head>
  <body>
    <main>
      <header class="page">
        <h1>Admin</h1>
        <div class="identity">
          {identity}
        </div>
      </header>
      <nav class="admin-nav">
        {nav}
        <a href="/">&larr; Back to home</a>
      </nav>
      <p>
        Welcome to the operator console. Pick a section above to inspect
        service health, integration status, or registered users.
      </p>
    </main>
  </body>
</html>
"""
)


_ADMIN_INTEGRATIONS_HTML = (
    """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin · integrations · ApplyPilot</title>
    <style>
"""
    + _BASE_CSS
    + """
    </style>
  </head>
  <body>
    <main>
      <header class="page">
        <h1>Integrations</h1>
        <div class="identity">
          {identity}
        </div>
      </header>
      <nav class="admin-nav">
        {nav}
        <a href="/">&larr; Back to home</a>
      </nav>
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Status</th>
            <th>Last checked</th>
            <th>Error</th>
            <th>Metadata</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </main>
  </body>
</html>
"""
)


def _format_integration_row(status: Any) -> str:
    """Render a single integration as a table row."""
    read = integration_status_to_read(status)
    status_class = _build_status_class(read.status)
    error = html.escape(read.error) if read.error else "<em>none</em>"
    metadata_block = _format_metadata(read.metadata)
    metadata_html = (
        f'<pre class="metadata">{html.escape(metadata_block)}</pre>'
        if metadata_block
        else "<em>none</em>"
    )
    return (
        "<tr>"
        f"<td><code>{html.escape(read.name)}</code></td>"
        f'<td><span class="{status_class}">{html.escape(read.status)}</span></td>'
        f"<td>{html.escape(_format_iso(read.last_checked_at))}</td>"
        f"<td>{error}</td>"
        f"<td>{metadata_html}</td>"
        "</tr>"
    )


_ADMIN_USERS_HTML = (
    """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin · users · ApplyPilot</title>
    <style>
"""
    + _BASE_CSS
    + """
    </style>
  </head>
  <body>
    <main>
      <header class="page">
        <h1>Users</h1>
        <div class="identity">
          {identity}
        </div>
      </header>
      <nav class="admin-nav">
        {nav}
        <a href="/">&larr; Back to home</a>
      </nav>
      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Created at</th>
            <th>Active</th>
            <th>Admin</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
      {pagination}
    </main>
  </body>
</html>
"""
)


def _format_user_row(user: User) -> str:
    """Render a single user as a table row."""
    return (
        "<tr>"
        f"<td>{html.escape(user.email)}</td>"
        f"<td>{html.escape(_format_iso(user.created_at))}</td>"
        f'<td><span class="{_BADGE_CLASS_BY_VALUE[user.is_active]}">'
        f"{'yes' if user.is_active else 'no'}</span></td>"
        f'<td><span class="{_BADGE_CLASS_BY_VALUE[user.is_admin]}">'
        f"{'yes' if user.is_admin else 'no'}</span></td>"
        "</tr>"
    )


_ADMIN_FORBIDDEN_HTML = (
    """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin · forbidden · ApplyPilot</title>
    <style>
"""
    + _BASE_CSS
    + """
    </style>
  </head>
  <body>
    <main>
      <header class="page">
        <h1>Admin</h1>
      </header>
      <p class="error-banner" role="alert">
        You are signed in, but this page is only available to admin users.
        Ask an operator to promote your account with
        <code>uv run apply-pilot promote --email &lt;your-email&gt;</code>,
        then refresh this page.
      </p>
      <p>
        <a href="/">&larr; Back to home</a>
      </p>
    </main>
  </body>
</html>
"""
)


# ---------------------------------------------------------------------------
# HTML auth gate
# ---------------------------------------------------------------------------


def _redirect_to_login(path: str) -> RedirectResponse:
    """Build a 303 redirect to the login page with ``?next=`` set."""
    target = f"{LOGIN_PATH}?next={path}"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


def _wants_html(request: Request) -> bool:
    """``True`` when the caller prefers HTML over JSON.

    Mirrors :func:`apply_pilot.features.users.api._wants_html`: a
    single ``text/html`` mention in ``Accept`` flips it on. Browser
    form submissions and the admin HTML page itself set this; the
    JSON admin endpoints do not.
    """
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept


def _json_error(status_code: int, code: str, message: str) -> Response:
    """Build a JSON-shaped error response for non-HTML callers."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )


def _resolve_or_redirect(
    request: Request,
    session: Session,
    *,
    path: str,
) -> Response | User:
    """Resolve the admin user, or return a Response (redirect / 403).

    Used by every HTML route. Differs from
    :func:`apply_pilot.features.admin._auth.resolve_admin_user` in
    three ways:

    * Unauthenticated requests get a 303 redirect to the login page
      (for HTML callers) or a 401 JSON error (for API callers). The
      content-type negotiation keeps the JSON admin endpoints'
      contract intact.
    * Stale / invalid tokens also get the same redirect / 401 split.
    * Authenticated-but-not-admin requests get a 403 — HTML page for
      browser visitors, JSON for API callers.

    The function returns either a :class:`User` (on success) or a
    :class:`fastapi.Response` (on auth failure). The route handler
    checks ``isinstance(result, User)`` and either renders the page
    or returns the response directly.
    """
    # Pull the bearer header from the request scope. We don't go
    # through FastAPI's ``Depends(HTTPBearer(...))`` here because
    # the HTML handler also accepts the session cookie as a fallback.
    from fastapi.security.utils import get_authorization_scheme_param

    auth_header = request.headers.get("authorization", "")
    scheme, bearer_token = get_authorization_scheme_param(auth_header)
    credentials: HTTPAuthorizationCredentials | None = None
    if scheme.lower() == "bearer" and bearer_token:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=bearer_token)
    token = _resolve_bearer_token(request, credentials)
    html_call = _wants_html(request)

    if token is None:
        if html_call:
            return _redirect_to_login(path)
        return _json_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    try:
        user_id_str = default_token_store().resolve(token)
    except InvalidTokenError:
        if html_call:
            return _redirect_to_login(path)
        return _json_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        )
    try:
        user_uuid = uuid.UUID(user_id_str)
    except (TypeError, ValueError):
        if html_call:
            return _redirect_to_login(path)
        return _json_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the token does not reference a valid user id",
        )

    user = session.get(User, user_uuid)
    if user is None or not user.is_active:
        if html_call:
            return _redirect_to_login(path)
        return _json_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the user behind the token no longer exists or is inactive",
        )
    if not user.is_admin:
        if html_call:
            return HTMLResponse(
                content=_ADMIN_FORBIDDEN_HTML,
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return _json_error(
            status.HTTP_403_FORBIDDEN,
            "admin_required",
            "this endpoint requires an admin user",
        )
    return user


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=True,
    responses={
        200: {"description": "Render the admin landing page"},
        303: {"description": "Unauthenticated; redirect to /auth/login"},
        403: {"description": "Authenticated but not an admin"},
    },
    summary="Admin landing page (HTML)",
)
async def admin_landing(
    request: Request,
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the admin landing page (M6, issue #171).

    Authentication gate (in priority order):

    1. No session cookie / bearer header → 303 to
       ``/auth/login?next=/admin/``.
    2. Credential resolves to a non-admin user → 403 HTML page.
    3. Credential resolves to an admin user → 200 HTML landing.
    """
    outcome = _resolve_or_redirect(request, session, path="/admin/")
    if isinstance(outcome, Response):
        return outcome
    user = outcome
    return HTMLResponse(
        content=_ADMIN_LANDING_HTML.format(
            identity=_build_identity(user),
            nav=_build_nav(active="home"),
        )
    )


@router.get(
    "/integrations",
    response_class=HTMLResponse,
    include_in_schema=True,
    responses={
        200: {"description": "Render the integrations HTML table"},
        303: {"description": "Unauthenticated; redirect to /auth/login"},
        403: {"description": "Authenticated but not an admin"},
    },
    summary="Admin integrations page (HTML)",
)
async def admin_integrations_page(
    request: Request,
    session: Session = Depends(get_db),  # noqa: B008
    store: IntegrationStatusStore = Depends(get_integration_status_store),  # noqa: B008
) -> Response:
    """Render the ``/admin/integrations`` HTML table (M6, issue #171)."""
    outcome = _resolve_or_redirect(request, session, path="/admin/integrations")
    if isinstance(outcome, Response):
        return outcome
    user = outcome

    statuses = store.get_all()
    if not statuses:
        rows_html = (
            '<tr><td colspan="5"><em>No integration snapshots yet. '
            "The integration status worker has not run, or the slice "
            "has no registered checkers.</em></td></tr>"
        )
    else:
        rows_html = "\n          ".join(_format_integration_row(s) for s in statuses)
    return HTMLResponse(
        content=_ADMIN_INTEGRATIONS_HTML.format(
            identity=_build_identity(user),
            nav=_build_nav(active="integrations"),
            rows=rows_html,
        )
    )


@router.get(
    "/users",
    response_class=HTMLResponse,
    include_in_schema=True,
    responses={
        200: {"description": "Render the paginated users HTML table"},
        303: {"description": "Unauthenticated; redirect to /auth/login"},
        403: {"description": "Authenticated but not an admin"},
    },
    summary="Admin users page (HTML, paginated)",
)
async def admin_users_page(
    request: Request,
    page: int = Query(1, ge=1, description="1-indexed page number"),
    size: int = Query(20, ge=1, le=100, description="Rows per page (max 100)"),
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the ``/admin/users`` paginated HTML table (M6, issue #171)."""
    outcome = _resolve_or_redirect(request, session, path="/admin/users")
    if isinstance(outcome, Response):
        return outcome
    user = outcome

    users_repo = SqlAlchemyUsersRepository(session=session)
    total = users_repo.count()
    offset = (page - 1) * size
    rows = users_repo.list_paginated(limit=size, offset=offset)

    if rows:
        rows_html = "\n          ".join(_format_user_row(r) for r in rows)
    else:
        rows_html = '<tr><td colspan="4"><em>No users match the current page.</em></td></tr>'

    pagination = _build_pagination(page=page, size=size, total=total, path="/admin/users")

    return HTMLResponse(
        content=_ADMIN_USERS_HTML.format(
            identity=_build_identity(user),
            nav=_build_nav(active="users"),
            rows=rows_html,
            pagination=pagination,
        )
    )


__all__ = [
    "admin_integrations_page",
    "admin_landing",
    "admin_users_page",
    "get_integration_status_store",
    "router",
]


# Suppress the unused-import warning for ``_bearer_scheme`` — it is
# kept re-exported so test files that already imported it from
# :mod:`apply_pilot.features.admin._auth` keep working.
_ = _bearer_scheme
