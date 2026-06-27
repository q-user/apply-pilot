"""Tests for ``Vacancy`` table index declarations.

Issue #259: ``vacancies.created_at`` is used in ``ORDER BY created_at DESC``
across three repository methods (``list_recent``, ``list_with_filters``,
``list_by_source``) and has no index, which forces a sequential scan plus
in-DB sort as the table grows.

This test pins the *model* definition: after ``Base.metadata.create_all``
runs, the ``vacancies`` table must declare the two indexes that back those
queries:

* ``ix_vacancies_created_at``               — covers ``list_recent`` and
  ``list_with_filters`` (order by created_at desc, no source filter).
* ``ix_vacancies_source_created_at``        — covers ``list_by_source``
  (filter by source, order by created_at desc). Putting ``source`` first
  in the composite means equality-on-source predicates use the index for
  both the lookup and the order.

The test is dialect-agnostic: it uses sqlite ``:memory:`` and SQLAlchemy's
``Inspector`` so the same assertion shape works for the PostgreSQL branch
as well.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from apply_pilot.db import Base
from apply_pilot.features.sources.models import Vacancy


def test_vacancies_table_has_created_at_index() -> None:
    """The single-column ``created_at`` index must be declared on Vacancy."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    index_names = {idx["name"] for idx in inspector.get_indexes("vacancies")}

    assert "ix_vacancies_created_at" in index_names, (
        f"expected ix_vacancies_created_at in {sorted(index_names)}"
    )


def test_vacancies_table_has_source_created_at_composite_index() -> None:
    """The composite (source, created_at) index must be declared on Vacancy.

    Backs ``list_by_source`` which does ``WHERE source = :s ORDER BY
    created_at DESC``. The composite order matters: putting ``source`` first
    lets the planner use the index for the equality predicate and the
    trailing ``created_at`` column for the sort.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    indexes = inspector.get_indexes("vacancies")
    by_name = {idx["name"]: idx for idx in indexes}

    assert "ix_vacancies_source_created_at" in by_name, (
        f"expected ix_vacancies_source_created_at in {sorted(by_name)}"
    )
    columns = by_name["ix_vacancies_source_created_at"]["column_names"]
    assert columns[:2] == ["source", "created_at"], (
        f"ix_vacancies_source_created_at should be (source, created_at); got {columns!r}"
    )


def test_vacancy_model_declares_both_indexes_via_table_args() -> None:
    """Defence-in-depth: the index entries must live on the model's metadata.

    ``Base.metadata.create_all`` emits the indexes, but if a future refactor
    rebuilds the model and forgets to copy them into ``__table_args__`` the
    test above would still pass (empty DB), so we additionally assert the
    declarations are present in the model's own ``__table_args__``.
    """
    from sqlalchemy import Index

    table_args = Vacancy.__table_args__
    # __table_args__ is either a tuple or a dict; normalise to an iterable of items.
    items = table_args.items() if isinstance(table_args, dict) else table_args

    declared_names = {arg.name for arg in items if isinstance(arg, Index)}

    assert "ix_vacancies_created_at" in declared_names
    assert "ix_vacancies_source_created_at" in declared_names
