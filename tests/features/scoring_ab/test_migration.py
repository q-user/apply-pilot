"""TDD test for the scoring_ab Alembic migration (issue #65).

Exercises the ``upgrade`` / ``downgrade`` round-trip on a fresh
sqlite database and asserts the three new tables exist with the
expected columns.

The test runs the migration through the alembic CLI but uses a
linear upgrade from a synthetic starting point so it does not
depend on the rest of the chain. The simplest reliable path is to
``alembic upgrade g7h8i9j0k1l2`` from a database that is already
at ``eb6c1c51520c`` (which is also the new migration's
``down_revision``). The test boots a full chain to reach
``eb6c1c51520c`` and then runs the new migration.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect


@pytest.fixture
def sqlite_file() -> Iterator[str]:
    """Yield the path of a temporary sqlite file. Removed at teardown."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


def _alembic_env(db_path: str) -> dict[str, str]:
    """Build the env for an ``alembic`` subprocess call."""
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path}"
    return env


def _alembic(*args: str, db_path: str) -> str:
    """Run the ``alembic`` CLI with the given arguments."""
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        env=_alembic_env(db_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {args} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result.stdout


def test_migration_upgrade_creates_three_tables(sqlite_file: str) -> None:
    """The migration upgrade must create the three new tables."""
    # Boot a full chain to reach the predecessor (``eb6c1c51520c``);
    # then upgrade to the new migration.
    _alembic("upgrade", "eb6c1c51520c", db_path=sqlite_file)
    _alembic("upgrade", "g7h8i9j0k1l2", db_path=sqlite_file)

    engine = create_engine(f"sqlite:///{sqlite_file}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "scoring_experiments" in tables
        assert "scoring_experiment_variants" in tables
        assert "scoring_experiment_outcomes" in tables
    finally:
        engine.dispose()


@pytest.mark.timeout(30)
def test_migration_downgrade_removes_three_tables(sqlite_file: str) -> None:
    """The migration downgrade must drop the three new tables.

    Marked with a per-test 30 s timeout because the test runs three
    sequential ``alembic`` subprocess invocations (each cold-starts
    ``uv`` and takes 1–2 s); under ``pytest -n auto`` the global
    5 s pytest-timeout is too tight when other workers compete for CPU.
    The downgrade itself completes in well under 1 s — the longer
    budget only gives the test room to breathe.
    """
    _alembic("upgrade", "eb6c1c51520c", db_path=sqlite_file)
    _alembic("upgrade", "g7h8i9j0k1l2", db_path=sqlite_file)
    _alembic("downgrade", "eb6c1c51520c", db_path=sqlite_file)

    engine = create_engine(f"sqlite:///{sqlite_file}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "scoring_experiments" not in tables
        assert "scoring_experiment_variants" not in tables
        assert "scoring_experiment_outcomes" not in tables
    finally:
        engine.dispose()
