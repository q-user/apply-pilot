"""Browser-friendly HTML renderer for the dashboard slice (M6, issue #172).

The dashboard already exposes JSON endpoints under ``GET /dashboard``
and ``GET /dashboard/funnel`` (and its two analytics siblings). This
module adds the HTML page that the M6 frontend shell links to from the
landing page.

Public surface
--------------

* :data:`router` — :class:`fastapi.APIRouter` mounted at ``/dashboard``
  with a single ``GET /`` route that renders the dashboard HTML for an
  authenticated browser user. The page is gated behind the same
  cookie / bearer credential contract the rest of the slice already
  uses; an unauthenticated request gets a ``303 See Other`` to
  ``/auth/login?next=/dashboard`` so a browser user never sees a JSON
  error response.
* :func:`render_dashboard_html` — the rendering helper. The
  :mod:`api` module calls into it for content-negotiated responses, and
  tests can call it directly without spinning up the FastAPI app.

The page is rendered with the same inline-HTML pattern
(:data:`_DASHBOARD_HTML`) used by :mod:`app` (``_LANDING_HTML``),
:mod:`features.admin.api` (``_HEALTH_PAGE_HTML``), and
:mod:`features.users.api` (``_LOGIN_HTML``) so every page in the
project looks like part of one design system. There is intentionally
no Jinja2 dependency: the slice is a thin read-only aggregator, and
the template is small enough that string interpolation beats the cost
of a template engine.
"""

from __future__ import annotations

import html
import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.apply_worker.models import ApplyJob, ApplyJobStatus
from apply_pilot.features.dashboard.schemas import (
    dashboard_summary_to_read,
)
from apply_pilot.features.dashboard.service import DashboardService
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.repository import SqlVacancyRepository
from apply_pilot.features.users.repository import SqlAlchemyUsersRepository
from apply_pilot.features.users.security import InvalidTokenError, default_token_store
from apply_pilot.features.users.session import LOGIN_PATH, get_session_token

if TYPE_CHECKING:
    from apply_pilot.features.users.models import User

_LOGGER = logging.getLogger("apply_pilot.features.dashboard.web")

router = APIRouter(prefix="/dashboard", tags=["dashboard-web"])

_bearer_scheme = HTTPBearer(auto_error=False)

#: Path the web router bounces unauthenticated visitors to. Kept as a
#: module constant so tests can compare against it without hardcoding
#: the string in every test.
LOGIN_NEXT: str = "/dashboard"


# ---------------------------------------------------------------------------
# Helpers — pure functions, easy to unit test
# ---------------------------------------------------------------------------


def _status_badge_class(status_value: str) -> str:
    """Return the CSS class for a given :class:`ApplyJobStatus` value.

    Mirrors the convention used by ``/admin/health``:

    * ``SUCCEEDED`` → ``healthy`` (green, ``--ok`` token).
    * ``FAILED`` / ``DEAD_LETTER`` → ``unhealthy`` (red, ``--bad``).
    * everything else (queued / running / cancelled) → ``neutral``
      (gray, ``--neutral`` token).
    """
    if status_value == ApplyJobStatus.SUCCEEDED.value:
        return "healthy"
    if status_value in {ApplyJobStatus.FAILED.value, ApplyJobStatus.DEAD_LETTER.value}:
        return "unhealthy"
    return "neutral"


def _format_iso(value: datetime | None) -> str:
    """Return an ISO-8601 string for *value* (UTC) or an em-dash for ``None``."""
    if value is None:
        return "—"
    # The model uses timezone-aware ``datetime``; emit the canonical
    # ``...+00:00`` form so the page renders the same on every locale.
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone().isoformat()


def _resolve_vacancy_id(
    jobs: Sequence[ApplyJob],
    *,
    vacancy_lookup: dict[uuid.UUID, Vacancy] | None,
) -> list[tuple[ApplyJob, str]]:
    """Pair each :class:`ApplyJob` with its vacancy-source id (as ``str``).

    When the underlying :class:`Vacancy` row is present in
    *vacancy_lookup*, the source's own identifier (``Vacancy.source_id``)
    is used; otherwise the bare ``str(ApplyJob.vacancy_id)`` (UUID form)
    stands in so the row is never rendered with an empty cell.

    The pairing keeps the renderer pure: it does not touch the database.
    """
    paired: list[tuple[ApplyJob, str]] = []
    for job in jobs:
        vacancy = vacancy_lookup.get(job.vacancy_id) if vacancy_lookup else None
        if vacancy is not None:
            paired.append((job, vacancy.source_id))
        else:
            paired.append((job, str(job.vacancy_id)))
    return paired


def _truncate(text: str | None, *, limit: int = 80) -> str:
    """Truncate *text* to *limit* characters and append an ellipsis.

    ``html.escape`` is intentionally **not** called here — the caller
    is responsible for escaping the result before interpolation.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _render_counts_grid(summary: object) -> str:
    """Render the five-card counts grid for the dashboard header.

    The grid is built from the public attributes of a
    :class:`DashboardSummary` (matches_by_status / applications_by_status)
    and the embedded digest :class:`UserStats` so a single round-trip
    populates the page.

    Returns an empty string when *summary* is ``None`` (defensive — the
    service always returns one today).
    """
    if summary is None:
        return ""
    matches_by_status = getattr(summary, "matches_by_status", {}) or {}
    digest = getattr(summary, "digest", None)

    # The digest already aggregates ``matches_new`` and
    # ``matches_review``; the per-status counts come straight from the
    # summary so the ``scored`` and ``accepted`` buckets line up with
    # the existing JSON contract.
    new_today = getattr(digest, "matches_new", 0) if digest is not None else 0
    seen = getattr(digest, "matches_review", 0) if digest is not None else 0
    scored = int(matches_by_status.get(MatchStatusValue.SCORED, 0))
    accepted = int(matches_by_status.get(MatchStatusValue.ACCEPTED, 0))
    applied = int(matches_by_status.get(MatchStatusValue.APPLIED, 0))

    cards = [
        ("Matches new today", new_today),
        ("Seen", seen),
        ("Scored", scored),
        ("Accepted", accepted),
        ("Applied", applied),
    ]
    parts = ['      <section class="counts" aria-label="Activity counts">']
    for label, value in cards:
        parts.append(
            f'        <div class="count-card">\n'
            f'          <div class="count-value">{int(value)}</div>\n'
            f'          <div class="count-label">{html.escape(label)}</div>\n'
            f"        </div>"
        )
    parts.append("      </section>")
    return "\n".join(parts)


class MatchStatusValue:
    """String constants for :class:`MatchStatus` values used in the HTML grid."""

    NEW = "new"
    SCORED = "scored"
    REVIEW = "review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    DEFERRED = "deferred"


def _render_recent_jobs_rows(
    jobs: Iterable[ApplyJob],
    *,
    vacancy_lookup: dict[uuid.UUID, Vacancy] | None,
) -> str:
    """Render the ``<tbody>`` content of the recent-apply-jobs table.

    Returns an empty-state ``<tr>`` when *jobs* is empty so the table
    is never collapsed / hidden. Every interpolated value is run
    through :func:`html.escape`.
    """
    jobs = list(jobs)
    if not jobs:
        return '          <tr class="empty"><td colspan="5">No recent activity</td></tr>'

    rows: list[str] = []
    for job, source_vacancy_id in _resolve_vacancy_id(jobs, vacancy_lookup=vacancy_lookup):
        badge_class = _status_badge_class(job.status)
        safe_status = html.escape(job.status)
        safe_vacancy = html.escape(source_vacancy_id)
        safe_created = html.escape(_format_iso(job.created_at))
        last_error = job.last_error or ""
        truncated = _truncate(last_error, limit=80)
        safe_truncated = html.escape(truncated) if truncated else "—"
        if last_error and last_error != truncated:
            safe_full = html.escape(last_error)
            error_cell = (
                f'            <details class="error-details">\n'
                f"              <summary>{safe_truncated}</summary>\n"
                f"              <pre>{safe_full}</pre>\n"
                f"            </details>"
            )
        else:
            error_cell = f"            {safe_truncated}"

        rows.append(
            f"          <tr>\n"
            f'            <td class="mono">{html.escape(str(job.id))}</td>\n'
            f'            <td><span class="status {badge_class}">{safe_status}</span></td>\n'
            f'            <td class="mono">{safe_vacancy}</td>\n'
            f'            <td class="mono">{safe_created}</td>\n'
            f"            <td>\n"
            f"{error_cell}\n"
            f"            </td>\n"
            f"          </tr>"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Inline HTML template (M6, issue #172)
# ---------------------------------------------------------------------------
#
# Style block mirrors ``_HEALTH_PAGE_HTML`` (admin/api.py) and
# ``_LOGIN_HTML`` (users/api.py) so every page in the project looks
# like part of one design system. The status badge classes reuse the
# same colour tokens (--ok / --warn / --bad / --neutral) the admin
# health page already exposes.
_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Dashboard · ApplyPilot</title>
    <style>
      :root {{
        color-scheme: light dark;
        --bg: #f7f7f8;
        --fg: #1f2328;
        --muted: #57606a;
        --card: #ffffff;
        --border: #d0d7de;
        --ok: #1a7f37;
        --warn: #9a6700;
        --bad: #cf222e;
        --neutral: #57606a;
        --accent: #0969da;
      }}
      @media (prefers-color-scheme: dark) {{
        :root {{
          --bg: #0d1117;
          --fg: #e6edf3;
          --muted: #8b949e;
          --card: #161b22;
          --border: #30363d;
          --ok: #3fb950;
          --warn: #d29922;
          --bad: #f85149;
          --neutral: #8b949e;
          --accent: #58a6ff;
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
      header.dash-header {{
        display: flex;
        flex-wrap: wrap;
        gap: 1rem;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 2rem;
      }}
      header.dash-header h1 {{
        font-size: 2rem;
        margin: 0;
        letter-spacing: -0.02em;
      }}
      header.dash-header .who {{
        color: var(--muted);
        font-size: 0.95rem;
      }}
      header.dash-header .actions {{
        display: flex;
        gap: 0.75rem;
        align-items: center;
      }}
      header.dash-header a {{
        color: var(--accent);
        text-decoration: none;
        font-size: 0.9rem;
      }}
      header.dash-header a:hover {{
        text-decoration: underline;
      }}
      form.signout {{
        display: inline;
        margin: 0;
      }}
      form.signout button {{
        background: transparent;
        border: 1px solid var(--border);
        color: var(--fg);
        font: inherit;
        padding: 0.4rem 0.75rem;
        border-radius: 6px;
        cursor: pointer;
      }}
      form.signout button:hover {{
        border-color: var(--accent);
        color: var(--accent);
      }}
      section.counts {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 0.75rem;
        margin-bottom: 2rem;
      }}
      .count-card {{
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 1rem 1.25rem;
        display: grid;
        gap: 0.25rem;
      }}
      .count-value {{
        font-size: 1.75rem;
        font-weight: 600;
        letter-spacing: -0.02em;
      }}
      .count-label {{
        color: var(--muted);
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }}
      section.recent h2 {{
        font-size: 1.25rem;
        margin: 0 0 0.75rem;
      }}
      table.recent {{
        width: 100%;
        border-collapse: collapse;
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 8px;
        overflow: hidden;
      }}
      table.recent th,
      table.recent td {{
        padding: 0.65rem 0.9rem;
        text-align: left;
        border-bottom: 1px solid var(--border);
        font-size: 0.9rem;
        vertical-align: top;
      }}
      table.recent th {{
        background: var(--bg);
        font-weight: 600;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        font-size: 0.75rem;
      }}
      table.recent tr:last-child td {{
        border-bottom: 0;
      }}
      table.recent td.mono {{
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
          Consolas, "Liberation Mono", monospace;
        font-size: 0.85rem;
        word-break: break-all;
      }}
      table.recent tr.empty td {{
        text-align: center;
        color: var(--muted);
        font-style: italic;
      }}
      .status {{
        display: inline-block;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        padding: 0.15rem 0.55rem;
        border-radius: 999px;
        border: 1px solid var(--border);
      }}
      .status.healthy {{
        color: var(--ok);
        border-color: color-mix(in srgb, var(--ok) 35%, transparent);
        background: color-mix(in srgb, var(--ok) 8%, transparent);
      }}
      .status.unhealthy {{
        color: var(--bad);
        border-color: color-mix(in srgb, var(--bad) 35%, transparent);
        background: color-mix(in srgb, var(--bad) 8%, transparent);
      }}
      .status.neutral {{
        color: var(--neutral);
        border-color: color-mix(in srgb, var(--neutral) 35%, transparent);
        background: color-mix(in srgb, var(--neutral) 8%, transparent);
      }}
      details.error-details summary {{
        cursor: pointer;
        color: var(--muted);
      }}
      details.error-details pre {{
        margin: 0.4rem 0 0;
        padding: 0.5rem 0.75rem;
        background: var(--bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
          Consolas, "Liberation Mono", monospace;
        font-size: 0.8rem;
        white-space: pre-wrap;
        word-break: break-word;
      }}
    </style>
  </head>
  <body>
    <main>
      <header class="dash-header">
        <div>
          <h1>Dashboard</h1>
          <div class="who">Logged in as {email}</div>
        </div>
        <div class="actions">
          <a href="/">&larr; Back to home</a>
          <form class="signout" method="post" action="/auth/logout">
            <button type="submit">Sign out</button>
          </form>
        </div>
      </header>
{counts_grid}
      <section class="recent" aria-label="Recent apply jobs">
        <h2>Recent apply jobs</h2>
        <table class="recent">
          <thead>
            <tr>
              <th scope="col">Job id</th>
              <th scope="col">Status</th>
              <th scope="col">Vacancy</th>
              <th scope="col">Created</th>
              <th scope="col">Last error</th>
            </tr>
          </thead>
          <tbody>
{recent_rows}
          </tbody>
        </table>
      </section>
    </main>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Auth / dispatch helpers
# ---------------------------------------------------------------------------


def _resolve_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Return the bearer token from the Authorization header OR the cookie.

    Header takes precedence (the bearer is the canonical credential);
    the session cookie is a fallback so a browser-based client can
    authenticate without any client-side JavaScript. Returns ``None``
    when neither is present.
    """
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    return get_session_token(request)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _resolve_session_user_id(request: Request) -> uuid.UUID | None:
    """Resolve the request's bearer / cookie token to a user id, or ``None``.

    ``None`` is the only "missing credential" signal — callers
    (:func:`render_dashboard_html`, :func:`dashboard_html_route`) use
    it to decide between a redirect (HTML path) and a 401 (JSON path).
    ``InvalidTokenError`` is treated as "missing" too so a stale
    cookie does not lock a browser visitor out of the page; the
    server still returns 303 to the login form.

    This is intentionally a separate helper from
    :func:`apply_pilot.features.users.api._resolve_bearer_token`
    because the dashboard web path wants to *swallow* invalid tokens
    rather than 401 — the redirect handles them transparently.
    """
    credentials: HTTPAuthorizationCredentials | None = None
    # We do not inject the bearer scheme through ``Depends`` here to
    # keep this helper independent of FastAPI's dependency machinery
    # (it is also called from the test layer via :func:`render_dashboard_html`).
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        from fastapi.security.utils import get_authorization_scheme_param

        scheme, token = get_authorization_scheme_param(auth_header)
        if scheme.lower() == "bearer" and token:
            credentials = HTTPAuthorizationCredentials(scheme="bearer", credentials=token)

    token = _resolve_bearer_token(request, credentials)
    if not token:
        return None
    tokens = default_token_store()
    try:
        return uuid.UUID(tokens.resolve(token))
    except (InvalidTokenError, ValueError, TypeError):
        return None


def _get_dashboard_service(session: Session) -> DashboardService:
    """Build a :class:`DashboardService` for the current request.

    Mirrors :func:`apply_pilot.features.dashboard.api.get_dashboard_service`
    but is local to this module so the web router does not have to
    import private members of the JSON router.
    """
    from apply_pilot.features.apply_worker.repository import SqlApplyJobRepository
    from apply_pilot.features.cover_letter.repository import (
        SqlCoverLetterDraftRepository,
    )
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository
    from apply_pilot.features.search_profiles.repository import (
        SqlSearchProfileRepository,
    )
    from apply_pilot.features.telegram.repository import (
        SqlAlchemyTelegramAccountRepository,
    )

    match_repo = SqlVacancyMatchRepository(session_factory=lambda: session)
    apply_job_repo = SqlApplyJobRepository(session_factory=lambda: session)
    cover_letter_repo = SqlCoverLetterDraftRepository(session=session)
    vacancy_repo = SqlVacancyRepository(session_factory=lambda: session)
    profile_repo = SqlSearchProfileRepository(session_factory=lambda: session)
    telegram_repo = SqlAlchemyTelegramAccountRepository(session=session)
    user_repo = SqlAlchemyUsersRepository(session=session)
    return DashboardService(
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        cover_letter_repo=cover_letter_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        telegram_account_repo=telegram_repo,
        user_repo=user_repo,
    )


def _vacancy_lookup(session: Session, jobs: Sequence[ApplyJob]) -> dict[uuid.UUID, Vacancy]:
    """Return ``{vacancy_id: Vacancy}`` for every distinct vacancy in *jobs*.

    The dashboard only renders the most-recent *N* jobs, so the lookup
    is bounded by *N* — no need to scan the full vacancy catalogue.
    Missing rows are simply absent from the mapping; the renderer
    falls back to the bare UUID string.
    """
    ids = {job.vacancy_id for job in jobs if job.vacancy_id is not None}
    if not ids:
        return {}
    repo = SqlVacancyRepository(session_factory=lambda: session)
    out: dict[uuid.UUID, Vacancy] = {}
    for vid in ids:
        row = repo.get_by_id(vid)
        if row is not None:
            out[vid] = row
    return out


# ---------------------------------------------------------------------------
# Public rendering helper (callable from tests + api.py)
# ---------------------------------------------------------------------------


def render_dashboard_html(
    *,
    user_id: uuid.UUID,
    service: DashboardService,
    session: Session,
) -> HTMLResponse:
    """Render the dashboard HTML for *user_id*.

    The function is the single rendering entry-point — both the
    :data:`router` and the JSON path's content-negotiated branch
    call into it so the page is always rendered the same way. It
    never raises on missing data: every ``getattr`` falls back to a
    zero / empty value so the page renders even when a slice
    dependency is unavailable.
    """
    user: User | None = SqlAlchemyUsersRepository(session=session).get_by_id(user_id)
    email = user.email if user is not None else "(unknown)"

    summary = service.get_summary(user_id)
    recent_jobs = service.get_recent_jobs(user_id=user_id, limit=10)
    vacancy_map = _vacancy_lookup(session, recent_jobs)

    safe_email = html.escape(email)
    counts_html = _render_counts_grid(summary)
    rows_html = _render_recent_jobs_rows(recent_jobs, vacancy_lookup=vacancy_map)

    body = _DASHBOARD_HTML.format(
        email=safe_email,
        counts_grid=counts_html,
        recent_rows=rows_html,
    )
    return HTMLResponse(content=body)


# ---------------------------------------------------------------------------
# FastAPI route
# ---------------------------------------------------------------------------


def _wants_html(request: Request) -> bool:
    """Return ``True`` when the request prefers HTML over JSON.

    Mirrors :func:`apply_pilot.features.users.api._wants_html` so the
    dashboard accepts the same content-negotiation contract the auth
    slice already uses for ``/auth/login``.
    """
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept


@router.get(
    "",
    response_model=None,
    include_in_schema=True,
    responses={
        200: {
            "description": (
                "Render the dashboard (HTML when Accept: text/html, JSON "
                "DashboardSummaryRead otherwise)."
            ),
        },
        303: {"description": "Unauthenticated HTML request; redirect to /auth/login."},
        401: {"description": "Missing or invalid bearer token (JSON request)."},
    },
    summary="Render the per-user dashboard (content-negotiated)",
)
def dashboard_route(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the dashboard HTML page (M6, issue #172).

    Content-negotiated: ``Accept: text/html`` (the browser default)
    renders the inline HTML page; ``Accept: application/json`` returns
    the same :class:`DashboardSummaryRead` payload :mod:`api` already
    ships, so curl / programmatic clients keep working.

    The endpoint accepts either the canonical ``Authorization: Bearer``
    header or the browser-friendly session cookie from PR #170. An
    unauthenticated HTML request is bounced to the login form with a
    ``next=/dashboard`` query string so the user lands back here
    after signing in; an unauthenticated JSON request returns the
    existing 401 JSON shape.
    """
    wants_html = _wants_html(request)
    token = _resolve_bearer_token(request, credentials)

    # No credentials at all: HTML path redirects, JSON path returns 401.
    if not token:
        if wants_html:
            return RedirectResponse(
                url=f"{LOGIN_PATH}?next={LOGIN_NEXT}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )

    tokens = default_token_store()
    try:
        user_id = uuid.UUID(tokens.resolve(token))
    except (InvalidTokenError, ValueError, TypeError) as exc:
        if wants_html:
            # A stale / revoked cookie on the HTML path is treated as
            # "no credential" so the user is bounced to the login form
            # rather than shown a JSON error page.
            return RedirectResponse(
                url=f"{LOGIN_PATH}?next={LOGIN_NEXT}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc

    service = _get_dashboard_service(session)

    if wants_html:
        return render_dashboard_html(
            user_id=user_id,
            service=service,
            session=session,
        )

    # JSON path: produce the same DashboardSummaryRead the original
    # /dashboard JSON handler returned. Keeps the existing curl /
    # programmatic contract intact.
    summary = service.get_summary(user_id)
    return JSONResponse(content=dashboard_summary_to_read(summary).model_dump(mode="json"))


__all__ = [
    "LOGIN_NEXT",
    "dashboard_route",
    "render_dashboard_html",
    "router",
]
