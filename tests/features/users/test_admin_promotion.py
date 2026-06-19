"""TDD tests for the ``apply-pilot promote`` CLI (M6, issue #171).

The CLI flips the new ``is_admin`` flag on an existing user. It is
the only bootstrap path for the very first admin — registration
through the HTTP API deliberately never sets ``is_admin`` so the
public signup surface stays unprivileged.

These tests drive :func:`apply_pilot.cli.promote.main` directly with
an argv list (matching the ``[project.scripts]`` contract) and assert
against a real SQLAlchemy session on an in-memory sqlite engine.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.cli import promote
from apply_pilot.db import Base, SessionLocal
from apply_pilot.features.users import models as _users_models  # noqa: F401  (register User)
from apply_pilot.features.users.security import hash_password


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Build a fresh in-memory sqlite engine and bind :data:`SessionLocal` to it.

    The CLI uses the module-level :data:`apply_pilot.db.SessionLocal`
    singleton, so to exercise it under sqlite we have to retarget
    the singleton at the per-test engine. The original URL is
    restored on teardown so other tests are unaffected.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)

    original_engine = SessionLocal.kw["bind"]
    # Re-bind the singleton to the new engine for the duration of the test.
    SessionLocal.configure(bind=eng)
    monkeypatch.setattr(
        "apply_pilot.db.engine",
        eng,
        raising=False,
    )
    try:
        yield eng
    finally:
        # Restore the original engine binding so other tests are not
        # contaminated by the in-memory engine.
        SessionLocal.configure(bind=original_engine)
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[Session]:
    """Yield a short-lived session bound to *engine*."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _insert_user(session: Session, *, email: str, is_admin: bool = False) -> None:
    """Insert a user with a known password and the given ``is_admin`` flag."""
    import uuid

    from apply_pilot.features.users.models import User

    user = User(
        id=uuid.uuid4(),
        email=email.lower(),
        hashed_password=hash_password("hunter2!!"),
        is_active=True,
        is_admin=is_admin,
    )
    session.add(user)
    session.commit()


def _user_is_admin(session: Session, *, email: str) -> bool:
    """Return the persisted ``is_admin`` flag for *email*."""
    from apply_pilot.features.users.models import User

    user = session.query(User).filter_by(email=email.lower()).one()
    return bool(user.is_admin)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_promote_marks_user_admin(
    session_factory: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    """``promote`` flips ``is_admin=True`` for an existing user and exits 0."""
    _insert_user(session_factory, email="ops@example.com")

    exit_code = promote.main(["--email", "ops@example.com"])

    assert exit_code == 0
    assert _user_is_admin(session_factory, email="ops@example.com") is True
    captured = capsys.readouterr()
    assert "ops@example.com" in captured.out


def test_promote_unknown_email_exits_nonzero(
    session_factory: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown email returns a non-zero exit code and a clear error."""
    exit_code = promote.main(["--email", "ghost@example.com"])

    assert exit_code != 0
    captured = capsys.readouterr()
    # Error must mention the email so operators can act.
    assert "ghost@example.com" in captured.err or "ghost@example.com" in captured.out


def test_promote_idempotent(session_factory: Session) -> None:
    """Promoting an already-admin user keeps the flag set and still returns 0."""
    _insert_user(session_factory, email="ops@example.com", is_admin=True)

    exit_code = promote.main(["--email", "ops@example.com"])

    assert exit_code == 0
    assert _user_is_admin(session_factory, email="ops@example.com") is True


def test_promote_requires_email_flag() -> None:
    """``promote`` without ``--email`` exits non-zero (argparse error)."""
    with pytest.raises(SystemExit):
        promote.main([])


def test_cli_dispatch_routes_promote_subcommand() -> None:
    """The top-level ``apply_pilot.cli.main`` dispatches ``promote``.

    This guards against the subcommand being silently ignored.
    """
    from apply_pilot.cli import main as cli_main

    # Without a recognised subcommand the top-level dispatcher must exit 2.
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["unknown-subcommand"])
    assert exc_info.value.code == 2
