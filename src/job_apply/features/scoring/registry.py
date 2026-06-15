"""Prompt-version registry for the scoring slice.

This module is the in-memory + SQL registry that maps prompt names
(``"vacancy_scoring"``, ``"cover_letter"``) to their versions and
tracks which version is "active" for each name. The future LLM
scoring pass (issue #29) will call :meth:`get_active` to pick the
template it should run a vacancy through.

The module exposes:

* :class:`PromptVersion` — frozen dataclass value object.
* :class:`PromptVersionRegistry` — :class:`typing.Protocol` every
  implementation satisfies.
* :class:`InMemoryPromptVersionRegistry` — dict-backed fake for tests.
* :class:`SqlPromptVersionRegistry` — SQLAlchemy-backed production
  implementation, with the partial UNIQUE index on
  ``(name) WHERE is_active`` enforcing the "one active per name"
  invariant at the DB level.
* :func:`seed_default_prompts` — registers the initial active versions
  for the two known prompt families.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from job_apply.features.scoring.models import PromptVersionRow

# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptVersion:
    """An immutable snapshot of a prompt template revision.

    Public surface (kept stable for downstream LLM scoring slices):

    * ``name`` — the prompt family.
    * ``version`` — SemVer string (e.g. ``"1.0.0"``, ``"1.2.0-rc.1"``).
    * ``template`` — the actual prompt body the LLM will receive.
    * ``is_active`` — exactly one row per ``name`` is active at a time.
    * ``created_at`` — server-side timestamp with timezone.
    """

    name: str
    version: str
    template: str
    is_active: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PromptVersionRegistry(Protocol):
    """Minimal contract the LLM scoring pipeline relies on.

    The future scoring pass (issue #29) only needs :meth:`get_active`
    to pick the right template. The remaining methods are public so
    operators and tests can audit / roll back versions.
    """

    def register(self, prompt: PromptVersion) -> PromptVersion: ...
    def get_active(self, name: str) -> PromptVersion | None: ...
    def get(self, name: str, version: str) -> PromptVersion | None: ...
    def list_all(self, name: str | None = None) -> list[PromptVersion]: ...
    def set_active(self, name: str, version: str) -> PromptVersion: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryPromptVersionRegistry:
    """Dict-backed registry for tests.

    Two indices make the lookup methods fast:

    * ``_by_pair`` — ``(name, version)`` → :class:`PromptVersion`.
    * ``_active_by_name`` — ``name`` → :class:`PromptVersion` (only the
      currently-active version lives here).

    The registry is a single-process fake: state is lost on restart,
    but the contract mirrors the SQL implementation closely enough to
    be drop-in for tests.
    """

    __slots__ = ("_by_pair", "_active_by_name")

    def __init__(self) -> None:
        self._by_pair: dict[tuple[str, str], PromptVersion] = {}
        self._active_by_name: dict[str, PromptVersion] = {}

    # -- writers ---------------------------------------------------------

    def register(self, prompt: PromptVersion) -> PromptVersion:
        """Insert a new prompt version.

        If ``prompt.is_active`` is ``True``, every other version of the
        same name that was previously active is deactivated in the same
        step so the "one active per name" invariant holds.
        """
        key = (prompt.name, prompt.version)
        if key in self._by_pair:
            raise ValueError(f"prompt version already registered: {prompt.name}@{prompt.version}")
        self._by_pair[key] = prompt
        if prompt.is_active:
            self._active_by_name[prompt.name] = prompt
        return prompt

    def set_active(self, name: str, version: str) -> PromptVersion:
        """Mark ``(name, version)`` as the active one; deactivate the rest."""
        key = (name, version)
        target = self._by_pair.get(key)
        if target is None:
            raise ValueError(f"prompt version not found: {name}@{version}")
        # Deactivate every previously active version of this name.
        for existing in list(self._by_pair.values()):
            if existing.name == name and existing.is_active and existing is not target:
                # ``PromptVersion`` is frozen, so we replace it with a
                # modified copy.
                self._by_pair[(existing.name, existing.version)] = PromptVersion(
                    name=existing.name,
                    version=existing.version,
                    template=existing.template,
                    is_active=False,
                    created_at=existing.created_at,
                )
        # Flip the target to active.
        activated = PromptVersion(
            name=target.name,
            version=target.version,
            template=target.template,
            is_active=True,
            created_at=target.created_at,
        )
        self._by_pair[(target.name, target.version)] = activated
        self._active_by_name[target.name] = activated
        return activated

    # -- readers ---------------------------------------------------------

    def get_active(self, name: str) -> PromptVersion | None:
        return self._active_by_name.get(name)

    def get(self, name: str, version: str) -> PromptVersion | None:
        return self._by_pair.get((name, version))

    def list_all(self, name: str | None = None) -> list[PromptVersion]:
        if name is None:
            return list(self._by_pair.values())
        return [p for p in self._by_pair.values() if p.name == name]


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


def _row_to_prompt(row: PromptVersionRow) -> PromptVersion:
    """Translate a :class:`PromptVersionRow` ORM row into the value object."""
    return PromptVersion(
        name=row.name,
        version=row.version,
        template=row.template,
        is_active=bool(row.is_active),
        created_at=row.created_at,
    )


class SqlPromptVersionRegistry:
    """SQLAlchemy-backed registry.

    The repository opens a short-lived session per operation and closes
    it before returning. The partial UNIQUE index on
    ``(name) WHERE is_active`` is the schema-level guarantee that two
    active versions of the same name can never coexist, even when two
    workers race to call :meth:`set_active` at the same time.
    """

    __slots__ = ("_session_factory",)

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session_factory is None:
            raise RuntimeError("SqlPromptVersionRegistry is not bound to a session")
        return self._session_factory()

    # -- writers ---------------------------------------------------------

    def register(self, prompt: PromptVersion) -> PromptVersion:
        """Insert a new prompt version row.

        If ``prompt.is_active`` is ``True``, the partial UNIQUE index
        will reject the insert when another active version of the same
        name already exists. The caller's contract is to use
        :meth:`set_active` for the "flip the active bit" workflow.
        """
        session = self._scope()
        try:
            row = PromptVersionRow(
                id=uuid.uuid4(),
                name=prompt.name,
                version=prompt.version,
                template=prompt.template,
                is_active=prompt.is_active,
                created_at=prompt.created_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_prompt(row)
        except IntegrityError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_active(self, name: str, version: str) -> PromptVersion:
        """Flip ``(name, version)`` to active; deactivate the rest.

        Done in a single transaction: the partial UNIQUE index would
        reject the second ``UPDATE`` otherwise, so the order matters.
        First deactivate every active version of the name, then
        activate the target.
        """
        session = self._scope()
        try:
            # Deactivate all currently-active versions of this name.
            session.execute(
                update(PromptVersionRow)
                .where(PromptVersionRow.name == name, PromptVersionRow.is_active.is_(True))
                .values(is_active=False)
            )
            # Activate the target. If the row does not exist, surface
            # ``ValueError`` for the caller to handle (the application
            # contract is "set_active on an unknown version is an
            # error", not a silent no-op).
            row = session.execute(
                select(PromptVersionRow).where(
                    PromptVersionRow.name == name,
                    PromptVersionRow.version == version,
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"prompt version not found: {name}@{version}")
            row.is_active = True
            session.commit()
            session.refresh(row)
            return _row_to_prompt(row)
        except ValueError:
            session.rollback()
            raise
        except IntegrityError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- readers ---------------------------------------------------------

    def get_active(self, name: str) -> PromptVersion | None:
        session = self._scope()
        try:
            row = session.execute(
                select(PromptVersionRow).where(
                    PromptVersionRow.name == name,
                    PromptVersionRow.is_active.is_(True),
                )
            ).scalar_one_or_none()
            return _row_to_prompt(row) if row is not None else None
        finally:
            session.close()

    def get(self, name: str, version: str) -> PromptVersion | None:
        session = self._scope()
        try:
            row = session.execute(
                select(PromptVersionRow).where(
                    PromptVersionRow.name == name,
                    PromptVersionRow.version == version,
                )
            ).scalar_one_or_none()
            return _row_to_prompt(row) if row is not None else None
        finally:
            session.close()

    def list_all(self, name: str | None = None) -> list[PromptVersion]:
        session = self._scope()
        try:
            statement = select(PromptVersionRow).order_by(
                PromptVersionRow.name, PromptVersionRow.version
            )
            if name is not None:
                statement = statement.where(PromptVersionRow.name == name)
            rows: Sequence[PromptVersionRow] = list(session.execute(statement).scalars().all())
            return [_row_to_prompt(r) for r in rows]
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


# The initial prompt templates. Kept here (not in a separate config
# file) so the seed is self-contained and easy to grep for. Future
# revisions are added by registering a new :class:`PromptVersion` with
# the same name and a higher SemVer string.
_VACANCY_SCORING_TEMPLATE = (
    "You are scoring a vacancy for fit against a candidate profile.\n"
    "Vacancy:\n{vacancy}\n\n"
    "Profile:\n{profile}\n\n"
    "Return a JSON object with keys: score (0-100), explanation (string)."
)
_COVER_LETTER_TEMPLATE = (
    "You are writing a cover letter for a vacancy.\n"
    "Vacancy:\n{vacancy}\n\n"
    "Profile:\n{profile}\n\n"
    "Style preferences:\n{style}\n\n"
    "Write a concise cover letter."
)


def seed_default_prompts(
    registry: PromptVersionRegistry,
    *,
    now: datetime | None = None,
) -> list[PromptVersion]:
    """Register the initial active prompt versions for the M3 slices.

    Idempotent: re-running the seed on a registry that already has the
    ``vacancy_scoring`` and ``cover_letter`` prompt names registered
    is a no-op (the existing active versions are left untouched).

    Returns the list of prompts the seed registered. When the registry
    already has active versions for the same names, the returned list
    is empty and the pre-existing rows are unchanged.
    """
    when = now or datetime.now(UTC)
    seeded: list[PromptVersion] = []
    candidates: list[PromptVersion] = [
        PromptVersion(
            name="vacancy_scoring",
            version="1.0.0",
            template=_VACANCY_SCORING_TEMPLATE,
            is_active=True,
            created_at=when,
        ),
        PromptVersion(
            name="cover_letter",
            version="1.0.0",
            template=_COVER_LETTER_TEMPLATE,
            is_active=True,
            created_at=when,
        ),
    ]
    for prompt in candidates:
        if registry.get_active(prompt.name) is not None:
            # Already seeded; leave the existing version untouched.
            continue
        try:
            registry.register(prompt)
        except (ValueError, IntegrityError):
            # Race with another seed: someone else registered it first.
            # The in-memory implementation raises ``ValueError``; the
            # SQL one raises ``IntegrityError`` for the same reason.
            continue
        seeded.append(prompt)
    return seeded


__all__ = [
    "InMemoryPromptVersionRegistry",
    "PromptVersion",
    "PromptVersionRegistry",
    "SqlPromptVersionRegistry",
    "seed_default_prompts",
]
