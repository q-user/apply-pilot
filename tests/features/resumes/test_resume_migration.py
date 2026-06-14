"""Roundtrip test for the resumes Alembic migration.

Runs ``alembic upgrade head`` and ``alembic downgrade base`` against a
disposable sqlite file and asserts that the ``resumes`` table exists with
the columns and constraints the ORM model expects. This is the only test
that exercises the migration end-to-end without touching the application's
default ``app.db`` file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _table_names(db_path: Path) -> set[str]:
    """Read sqlite master table names without keeping a connection open."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _resumes_columns(db_path: Path) -> set[str]:
    """Return the column names of the resumes table in ``db_path``."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA table_info(resumes)").fetchall()
    finally:
        conn.close()
    return {row[1] for row in rows}


def _resumes_foreign_keys(db_path: Path) -> list[tuple[str, str, str]]:
    """Return (from_column, to_table, on_delete) for the resumes FKs."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA foreign_key_list(resumes)").fetchall()
    finally:
        conn.close()
    # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
    return [(row[3], row[2], row[6]) for row in rows]


def test_resumes_alembic_upgrade_creates_table(tmp_path: Path) -> None:
    """``alembic upgrade head`` creates the resumes table with the expected columns."""
    db_path = tmp_path / "resumes_migration.db"
    url = f"sqlite+pysqlite:///{db_path}"
    # The migration test lives at tests/features/resumes/, so the repo
    # root is three levels up.
    repo_root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(repo_root / "src")}
    python = sys.executable

    result = subprocess.run(
        [python, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "resumes" in _table_names(db_path)
    columns = _resumes_columns(db_path)
    expected = {
        "id",
        "user_id",
        "filename",
        "content_type",
        "size",
        "raw_text",
        "plain_text",
        "created_at",
        "updated_at",
    }
    assert expected <= columns
    fks = _resumes_foreign_keys(db_path)
    assert ("user_id", "users", "CASCADE") in fks


def test_resumes_alembic_downgrade_drops_table(tmp_path: Path) -> None:
    """``alembic downgrade base`` removes the resumes table."""
    db_path = tmp_path / "resumes_migration.db"
    url = f"sqlite+pysqlite:///{db_path}"
    repo_root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(repo_root / "src")}
    python = sys.executable

    # Upgrade to head first
    subprocess.run(
        [python, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "resumes" in _table_names(db_path)

    # Then downgrade to base (before the resumes migration)
    result = subprocess.run(
        [python, "-m", "alembic", "downgrade", "c31323bea8d1"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic downgrade failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "resumes" not in _table_names(db_path)
