"""Data-driven field coverage for ``InMemoryVacancyRepository.upsert``.

Issue #265: the old implementation hard-coded a 16-tuple of attribute
names. Adding a new column to the ``Vacancy`` model required manually
editing that tuple, and the SQL implementation derived the same set from
``_upsert_columns()`` — so the in-memory and SQL repositories could
silently drift apart.

These tests assert that a brand-new column added to ``Vacancy.__table__``
is propagated by ``upsert`` from the incoming record to the existing
record without any further code change. They swap in an extended
``Table`` for the duration of the test and restore the original on
teardown so the global ``Vacancy`` model is never polluted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, MetaData, String, Table

from apply_pilot.features.audit import models as _audit_models  # noqa: F401
from apply_pilot.features.resumes import models as _resumes_models  # noqa: F401
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.repository import InMemoryVacancyRepository


def _extend_vacancy_table(new_column_name: str) -> Table:
    """Return a fresh ``Table`` mirroring ``Vacancy.__table__`` plus one extra column.

    Uses a private :class:`MetaData` (not ``Base.metadata``) so the swap
    never pollutes the shared registry: the in-memory repository only
    consults ``Vacancy.__table__.columns`` for the upsert field set, so
    the extra column does not need to live in the application's metadata.
    """
    extended = Table("vacancies", MetaData())
    for column in Vacancy.__table__.columns:
        # Reconstruct each column against the new table. ``copy.copy``
        # would carry over the original ``table`` reference and trip
        # SQLAlchemy's parent-table guard on append.
        cloned = Column(
            column.name,
            column.type,
            *column.constraints,
            primary_key=column.primary_key,
            nullable=column.nullable,
            default=column.default,
            server_default=column.server_default,
        )
        extended.append_column(cloned, replace_existing=True)
    extended.append_column(
        Column(new_column_name, String(255), nullable=True),
    )
    return extended


@pytest.fixture
def vacancy_with_extra_column(monkeypatch: pytest.MonkeyPatch):
    """Swap ``Vacancy.__table__`` with one that has an extra column.

    Yields the new column's name. The original table is restored on
    teardown, so subsequent tests see the unchanged model.
    """
    column_name = "extra_marker_field"
    original_table = Vacancy.__table__
    extended_table = _extend_vacancy_table(column_name)
    monkeypatch.setattr(Vacancy, "__table__", extended_table)
    try:
        yield column_name
    finally:
        # Defensive: ensure original is restored even if monkeypatch
        # somehow lost the cleanup hook.
        Vacancy.__table__ = original_table  # type: ignore[assignment]


def _make_vacancy(source_id: str, **overrides) -> Vacancy:
    payload: dict = {
        "source": "hh",
        "source_id": source_id,
        "title": "Python Dev",
        "location": "Moscow",
        "salary_from": 100000,
        "salary_to": 200000,
        "salary_currency": "RUR",
        "salary_gross": False,
        "raw_data": {"id": source_id, "name": "Python Dev"},
    }
    payload.update(overrides)
    return Vacancy(**payload)


class TestInMemoryUpsertIsDataDriven:
    def test_new_column_propagates_on_existing_row(
        self,
        vacancy_with_extra_column: str,
    ) -> None:
        """A new column added to Vacancy must be copied to the existing row."""
        column_name = vacancy_with_extra_column

        repo = InMemoryVacancyRepository()
        first = repo.upsert(_make_vacancy("hh-1"))
        original_id = first.id
        original_created_at = first.created_at
        assert original_id is not None

        incoming = _make_vacancy("hh-1", title="Python Dev (senior)")
        # Attach the new attribute on the instance so the in-memory repo
        # has something to copy across. ``Vacancy.__init__`` ignores
        # unknown kwargs (SQLAlchemy declarative does, too), so we set
        # the attribute directly.
        setattr(incoming, column_name, "marker-value")

        result = repo.upsert(incoming)

        # Identifiers and audit timestamps are preserved.
        assert result.id == original_id
        assert result.created_at == original_created_at
        assert result.updated_at is not None
        # Mutated field still updates.
        assert result.title == "Python Dev (senior)"
        # The new column is propagated to the persisted row.
        assert getattr(result, column_name) == "marker-value"

    def test_new_column_propagates_to_persisted_lookup(
        self,
        vacancy_with_extra_column: str,
    ) -> None:
        """The stored record is mutated, not just the returned reference."""
        column_name = vacancy_with_extra_column

        repo = InMemoryVacancyRepository()
        repo.upsert(_make_vacancy("hh-1"))

        incoming = _make_vacancy("hh-1", title="Python Dev (updated)")
        setattr(incoming, column_name, "another-marker")
        repo.upsert(incoming)

        stored = repo.get_by_id(next(iter(repo._by_id.values())).id)  # type: ignore[attr-defined]
        assert stored is not None
        assert getattr(stored, column_name) == "another-marker"
        assert stored.title == "Python Dev (updated)"
