"""Roundtrip test for the writing-style-memory Alembic migration.

Runs ``alembic upgrade head`` and ``alembic downgrade base`` against a
disposable sqlite file and asserts that the ``style_memory_entries``
table exists with the columns and constraints the ORM model expects.
This is the only test that exercises the migration end-to-end without
touching the application's default ``app.db`` file.
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


def _style_memory_columns(db_path: Path) -> set[str]:
    """Return the column names of the style_memory_entries table in ``db_path``."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA table_info(style_memory_entries)").fetchall()
    finally:
        conn.close()
    return {row[1] for row in rows}


def _style_memory_foreign_keys(db_path: Path) -> list[tuple[str, str, str]]:
    """Return (from_column, to_table, on_delete) for the style_memory FKs."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA foreign_key_list(style_memory_entries)").fetchall()
    finally:
        conn.close()
    # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
    return [(row[3], row[2], row[6]) for row in rows]


def test_style_memory_alembic_upgrade_creates_table(tmp_path: Path) -> None:
    """``alembic upgrade head`` creates the style_memory_entries table with the expected columns."""
    db_path = tmp_path / "style_memory_migration.db"
    url = f"sqlite+pysqlite:///{db_path}"
    # The migration test lives at tests/features/writing_style_memory/, so
    # the repo root is three levels up.
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
    assert "style_memory_entries" in _table_names(db_path)
    columns = _style_memory_columns(db_path)
    expected = {
        "id",
        "user_id",
        "cover_letter_id",
        "letter_text",
        "style_summary",
        "created_at",
    }
    assert expected <= columns
    fks = _style_memory_foreign_keys(db_path)
    assert ("user_id", "users", "CASCADE") in fks
    assert ("cover_letter_id", "cover_letter_drafts", "SET NULL") in fks


def test_style_memory_alembic_downgrade_drops_table(tmp_path: Path) -> None:
    """``alembic downgrade`` removes the style_memory_entries table.

    Originally the migration was a merge node with two parents
    (``eb6c1c51520c`` and ``f1a2b3c4d5e6``); the test used to downgrade
    to the ``f1a2b3c4d5e6`` parent and assert the table is gone. M10
    (issue #204) dropped the ``f1a2b3c4d5e6`` migration, so the
    ``71a2b3c4d5e6`` revision now has a single parent. We downgrade
    to ``eb6c1c51520c`` instead.
    """
    db_path = tmp_path / "style_memory_migration.db"
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
    assert "style_memory_entries" in _table_names(db_path)

    # Downgrade to the parent. Was ``f1a2b3c4d5e6`` (the other merge
    # parent) before M10 dropped that revision.
    result = subprocess.run(
        [python, "-m", "alembic", "downgrade", "eb6c1c51520c"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic downgrade failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "style_memory_entries" not in _table_names(db_path)
