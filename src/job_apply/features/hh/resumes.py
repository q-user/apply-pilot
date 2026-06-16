"""hh.ru resume metadata sync (M2, issue #21).

This module is the boundary between the application and hh.ru's
``/resumes/mine`` and ``/resumes/{id}`` endpoints. It exposes:

* :class:`HhResumesClient` — the :class:`typing.Protocol` every
  collaborator depends on. Return values are raw hh.ru payloads
  (``dict``); the protocol is intentionally format-agnostic so callers
  can swap in a cached or mocked transport without touching the service.
* :class:`InMemoryHhResumesClient` — a fixture-backed fake used by
  tests and local development.
* :class:`HhHttpResumesClient` — the production client backed by
  :mod:`httpx`. Bearer tokens are attached via an injectable
  ``token_provider`` so the client itself never has to know about
  :class:`~job_apply.features.hh.service.HHCredentialService`.
* :class:`HhResumeLink` — the ORM model that tracks the link between
  a local user and an hh resume.
* :class:`HhResumeLinkRepository` — the narrow persistence contract
  the service depends on, with both an in-memory and a SQL
  implementation.
* :class:`HhResumesSyncService` — the orchestrator that pulls the
  user's resume metadata from hh.ru and upserts it into the
  :class:`HhResumeLink` table.

The slice keeps ORM coupling out of the HTTP client and token
resolution out of the service: the FastAPI route builds a per-user
``token_provider`` closure over :class:`HHCredentialService` and passes
the resulting :class:`HhHttpResumesClient` to the service. That way the
service has no idea *how* the bearer token was obtained, and the client
has no idea *which* user it is talking to.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_apply.db import Base
from job_apply.shared.errors import DomainError
from job_apply.shared.types import GUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HhResumesError(DomainError):
    """Base error for hh.ru resume metadata sync failures."""

    code: str = "hh_resumes_error"


class HhResumeNotFoundError(HhResumesError):
    """The requested hh resume id does not exist (HTTP 404)."""

    code: str = "hh_resume_not_found"


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class HhResumeLink(Base):
    """A link between a local :class:`~job_apply.features.users.models.User`
    and one of their hh.ru resumes.

    A single hh resume maps to at most one :class:`HhResumeLink` per user
    (the ``(user_id, hh_resume_id)`` unique constraint enforces this).
    The ``local_resume_id`` is nullable: hh resumes are synced as
    metadata *first*; full-text fetch (and a link to a local
    :class:`~job_apply.features.resumes.models.Resume` row) is out of
    scope for M2 and lands in a later slice.
    """

    __tablename__ = "hh_resume_links"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", name="fk_hh_resume_links_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: FK to ``resumes.id`` (owned by the resumes slice, issue #12). The
    #: FK is ``ondelete=SET NULL`` so a hard-deleted local resume does
    #: not take its hh link with it — the link still tracks the
    #: external id, even if the local content is gone.
    local_resume_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("resumes.id", name="fk_hh_resume_links_local_resume_id", ondelete="SET NULL"),
        nullable=True,
    )
    hh_resume_id: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at_hh: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "hh_resume_id", name="uq_hh_resume_links_user_hh_resume"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"HhResumeLink(id={self.id!s}, user_id={self.user_id!s}, "
            f"hh_resume_id={self.hh_resume_id!r}, title={self.title!r})"
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class HhResumesClient(Protocol):
    """The narrow contract every hh resumes collaborator depends on.

    Return values are raw hh.ru payloads (``dict``). The protocol is
    intentionally format-agnostic so a cached or mocked transport can
    drop in without changing callers.
    """

    async def list_user_resumes(self) -> list[dict]: ...

    async def get_resume(self, hh_resume_id: str) -> dict: ...


# ---------------------------------------------------------------------------
# In-memory client
# ---------------------------------------------------------------------------


class InMemoryHhResumesClient:
    """Dict-backed fake used by tests and local development.

    ``fixtures`` is a list of *batches* — each call to
    :meth:`list_user_resumes` returns the first batch. Tests that need
    to simulate a re-sync call :meth:`replace_fixtures` to swap the
    active batch. An empty list (the default) makes
    :meth:`list_user_resumes` return ``[]`` — matching hh.ru's
    zero-resumes response.
    """

    def __init__(self, fixtures: list[list[dict]] | None = None) -> None:
        self._batches: list[list[dict]] = list(fixtures or [[]])
        self._batch_index = 0

    async def list_user_resumes(self) -> list[dict]:
        """Return the active fixture batch verbatim."""
        if not self._batches:
            return []
        batch = self._batches[self._batch_index % len(self._batches)]
        return [dict(item) for item in batch]

    async def get_resume(self, hh_resume_id: str) -> dict:
        """Look up a resume by id across every fixture batch."""
        for batch in self._batches:
            for item in batch:
                if str(item.get("id")) == hh_resume_id:
                    return dict(item)
        raise HhResumeNotFoundError(f"hh resume {hh_resume_id!r} not found in fixtures")

    def replace_fixtures(self, fixtures: list[list[dict]]) -> None:
        """Swap the fixture list. Used by tests that simulate a re-sync."""
        self._batches = list(fixtures or [[]])
        self._batch_index = 0


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


#: Resolves a bearer token for the current request, or returns ``None``
#: if no credentials are stored. The callable takes no arguments —
#: the closure binds the user at construction time so a single client
#: instance can be reused across requests (production) or injected per
#: request (the FastAPI wiring does the latter).
HhResumesTokenProvider = Callable[[], str | None]


def _noop_token_provider() -> str | None:
    """Default :data:`HhResumesTokenProvider` that always returns ``None``."""
    return None


def _parse_updated_at(value: Any) -> datetime | None:
    """Best-effort parse of hh.ru's ``updated_at`` ISO-8601 string.

    Returns ``None`` if the value is missing or cannot be parsed; the
    service treats ``None`` as "unknown" and stores it as-is.
    """
    if not value:
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # hh.ru always returns tz-aware timestamps, but a defensive
        # fallback keeps the service robust against an upstream change.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class HhHttpResumesClient:
    """Production :class:`HhResumesClient` backed by :mod:`httpx`.

    The HTTP client is *injected* — callers (production wiring, tests)
    own its lifetime. This keeps the adapter testable with
    :class:`httpx.MockTransport` and lets the application share a
    pooled :class:`httpx.AsyncClient` across slices.

    The bearer token is fetched lazily via ``token_provider`` on every
    request, so a long-lived client can still pick up refreshed tokens
    without being rebuilt. The provider is a closure built by the
    FastAPI route: ``lambda: credential_service.get_credentials(user_id).access_token``.
    """

    DEFAULT_USER_AGENT: str = "ApplyPilot/0.1"
    DEFAULT_BASE_URL: str = "https://api.hh.ru"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        token_provider: HhResumesTokenProvider | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._token_provider: HhResumesTokenProvider = token_provider or _noop_token_provider

    # ------------------------------------------------------------------
    # HhResumesClient
    # ------------------------------------------------------------------

    async def list_user_resumes(self) -> list[dict]:
        """Fetch ``GET /resumes/mine`` and return the ``items`` array."""
        response = await self._client.get(
            f"{self._base_url}/resumes/mine",
            headers=self._build_headers(),
        )
        return self._parse_list_response(response)

    async def get_resume(self, hh_resume_id: str) -> dict:
        """Fetch a single resume by hh.ru id and return the JSON body.

        Raises:
            HhResumeNotFoundError: If hh.ru returns 404.
            HhResumesError: For any other 4xx/5xx response.
        """
        response = await self._client.get(
            f"{self._base_url}/resumes/{hh_resume_id}",
            headers=self._build_headers(),
        )
        self._raise_for_status(response)
        try:
            return response.json()
        except ValueError as exc:
            raise HhResumesError(
                f"hh.ru returned a non-JSON body for resume {hh_resume_id!r}: {exc!s}",
            ) from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Assemble the outgoing request headers.

        The bearer token is fetched via ``token_provider`` on every
        request, so refreshed credentials are picked up without
        rebuilding the client.
        """
        headers: dict[str, str] = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        token = self._token_provider()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _parse_list_response(self, response: httpx.Response) -> list[dict]:
        """Validate an hh.ru ``/resumes/mine`` response and return its items."""
        self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise HhResumesError(
                f"hh.ru returned a non-JSON body: {exc!s}",
            ) from exc
        items = data.get("items")
        if not isinstance(items, list):
            raise HhResumesError("hh.ru response is missing the 'items' array")
        return items

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Translate an hh.ru error into a typed domain error."""
        status = response.status_code
        if status == httpx.codes.NOT_FOUND:
            raise HhResumeNotFoundError(
                f"hh.ru returned 404 for {response.url!s}",
            )
        if status >= 400:
            raise HhResumesError(
                f"hh.ru resumes request failed with HTTP {status}: {response.text[:200]!r}",
            )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class HhResumeLinkRepository(Protocol):
    """Minimal interface :class:`HhResumesSyncService` relies on."""

    def upsert(self, link: HhResumeLink) -> HhResumeLink: ...
    def list_by_user(self, user_id: uuid.UUID) -> list[HhResumeLink]: ...


class InMemoryHhResumeLinkRepository:
    """Dict-backed repository for tests.

    A fresh instance per test keeps isolation simple. The
    ``(user_id, hh_resume_id)`` index mirrors the SQL unique constraint
    so :meth:`upsert` matches existing rows the same way the SQL
    implementation does.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, HhResumeLink] = {}
        self._by_pair: dict[tuple[uuid.UUID, str], uuid.UUID] = {}

    def upsert(self, link: HhResumeLink) -> HhResumeLink:
        existing_id = self._by_pair.get((link.user_id, link.hh_resume_id))
        now = datetime.now(UTC)
        if existing_id is not None:
            existing = self._by_id[existing_id]
            # Preserve identity and the original created_at; refresh
            # everything else from the new link and bump updated_at.
            existing.local_resume_id = link.local_resume_id
            existing.title = link.title
            existing.updated_at_hh = link.updated_at_hh
            existing.last_synced_at = link.last_synced_at
            existing.updated_at = now
            return existing
        if link.id is None:
            link.id = uuid.uuid4()
        if link.created_at is None:
            link.created_at = now
        if link.last_synced_at is None:
            link.last_synced_at = now
        self._by_id[link.id] = link
        self._by_pair[(link.user_id, link.hh_resume_id)] = link.id
        return link

    def list_by_user(self, user_id: uuid.UUID) -> list[HhResumeLink]:
        """Return every link for *user_id*, oldest first by created_at."""
        rows = [link for link in self._by_id.values() if link.user_id == user_id]
        rows.sort(key=lambda link: (link.created_at, str(link.id)))
        return rows


class SqlHhResumeLinkRepository:
    """SQLAlchemy-backed repository for :class:`HhResumeLink`.

    Follows the same dual-construction pattern as
    :class:`SqlHHCredentialRepository`: pass either ``session`` (caller-
    managed) or ``session_factory`` (per-operation lifecycle).
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise ValueError("pass either session or session_factory, not both")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlHhResumeLinkRepository is not bound to a session")
        return self._session_factory()

    def upsert(self, link: HhResumeLink) -> HhResumeLink:
        """Insert or update a :class:`HhResumeLink` keyed by ``(user_id, hh_resume_id)``.

        The upsert matches on the unique constraint, so calling this
        twice with the same ``(user_id, hh_resume_id)`` is idempotent
        and returns the same row id both times.
        """
        scoped = self._scope()
        try:
            existing = scoped.execute(
                select(HhResumeLink).where(
                    HhResumeLink.user_id == link.user_id,
                    HhResumeLink.hh_resume_id == link.hh_resume_id,
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if existing is not None:
                existing.local_resume_id = link.local_resume_id
                existing.title = link.title
                existing.updated_at_hh = link.updated_at_hh
                existing.last_synced_at = now
                scoped.commit()
                scoped.refresh(existing)
                return existing

            if link.id is None:
                link.id = uuid.uuid4()
            if link.created_at is None:
                link.created_at = now
            if link.last_synced_at is None:
                link.last_synced_at = now
            scoped.add(link)
            scoped.commit()
            scoped.refresh(link)
            return link
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def list_by_user(self, user_id: uuid.UUID) -> list[HhResumeLink]:
        """Return every link for *user_id*, oldest first by created_at."""
        scoped = self._scope()
        try:
            statement = (
                select(HhResumeLink)
                .where(HhResumeLink.user_id == user_id)
                .order_by(HhResumeLink.created_at.asc(), HhResumeLink.id.asc())
            )
            return list(scoped.execute(statement).scalars().all())
        finally:
            if self._session is None:
                scoped.close()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class HhResumesSyncService:
    """Orchestrator that pulls resume metadata from hh.ru and persists it.

    The service is constructed per-request by the FastAPI route. The
    injected :class:`HhResumesClient` is expected to be pre-configured
    with a ``token_provider`` closure that uses this service's
    ``credential_service`` and ``user_id`` to mint a fresh access
    token — the service itself does not need to know *how* the token
    is fetched.
    """

    def __init__(
        self,
        *,
        resumes_client: HhResumesClient,
        credential_service: Any,
        link_repo: HhResumeLinkRepository,
        user_id: uuid.UUID,
    ) -> None:
        self._resumes_client = resumes_client
        self._credential_service = credential_service
        self._link_repo = link_repo
        self._user_id = user_id

    async def sync_metadata(self) -> list[HhResumeLink]:
        """Fetch the user's resumes from hh.ru and upsert every link.

        Returns the upserted :class:`HhResumeLink` rows in the order
        hh.ru returned them. A user with zero resumes on hh.ru yields
        an empty list and writes nothing.
        """
        # Touch the credential service to fail fast if the user has
        # not linked their hh.ru account yet — better to surface a
        # 404/typed error than to make an unauthenticated call.
        try:
            self._credential_service.get_credentials(self._user_id)
        except Exception:
            logger.warning(
                "hh.resumes.sync.no_credentials",
                extra={"user_id": str(self._user_id)},
            )
            raise

        items = await self._resumes_client.list_user_resumes()
        now = datetime.now(UTC)
        synced: list[HhResumeLink] = []
        for item in items:
            hh_resume_id = str(item.get("id")) if item.get("id") is not None else ""
            if not hh_resume_id:
                # hh.ru guarantees ``id`` on every item; a missing id
                # is a contract violation we cannot persist.
                logger.warning(
                    "hh.resumes.sync.skip_missing_id",
                    extra={"user_id": str(self._user_id), "item": item},
                )
                continue
            link = HhResumeLink(
                user_id=self._user_id,
                hh_resume_id=hh_resume_id,
                title=_safe_str(item.get("title"), max_length=255),
                updated_at_hh=_parse_updated_at(item.get("updated_at")),
                last_synced_at=now,
            )
            synced.append(self._link_repo.upsert(link))
        return synced


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_str(value: Any, *, max_length: int) -> str | None:
    """Return ``value`` as a string, truncated to *max_length*.

    ``None`` and non-string inputs map to ``None`` so the database
    column stays NULL rather than holding a useless empty string.
    """
    if value is None or not isinstance(value, str):
        return None
    return value[:max_length]


__all__ = [
    "HhHttpResumesClient",
    "HhResumeLink",
    "HhResumeLinkRepository",
    "HhResumeNotFoundError",
    "HhResumesClient",
    "HhResumesError",
    "HhResumesSyncService",
    "HhResumesTokenProvider",
    "InMemoryHhResumeLinkRepository",
    "InMemoryHhResumesClient",
    "SqlHhResumeLinkRepository",
]
