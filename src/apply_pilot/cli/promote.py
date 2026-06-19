"""``apply-pilot promote`` sub-command (M6, issue #171).

The only bootstrap path for the very first admin user. The HTTP
``POST /auth/register`` endpoint deliberately never sets ``is_admin``,
so a freshly-registered user is unprivileged regardless of how many
users are in the system. An operator with shell access runs::

    uv run apply-pilot promote --email you@example.com

and the matching user row is updated in place. The command is
idempotent: running it twice is harmless (the second run is a no-op
write).

The CLI opens its own SQLAlchemy session via
:func:`apply_pilot.db.get_session_local` so it shares the same
database the running API uses. The session is closed in a ``finally``
block regardless of the outcome.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy.orm import Session

from apply_pilot.db import SessionLocal
from apply_pilot.features.users.repository import SqlAlchemyUsersRepository


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``promote`` sub-command on *subparsers*."""
    parser = subparsers.add_parser(
        "promote",
        help="Promote a registered user to admin (sets is_admin=True).",
        description=(
            "Look up a user by email and flip their is_admin flag to True. "
            "Idempotent: re-running on an already-admin user is a no-op write."
        ),
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email address of the user to promote.",
    )


def run(args: argparse.Namespace) -> int:
    """Execute the ``promote`` command. Returns the process exit code."""
    session: Session = SessionLocal()
    try:
        repo = SqlAlchemyUsersRepository(session=session)
        user = repo.get_by_email(args.email)
        if user is None:
            print(
                f"error: no user found with email {args.email!r}",
                file=sys.stderr,
            )
            return 1
        if user.is_admin:
            print(f"user {args.email!r} is already an admin; nothing to do.")
            return 0
        user.is_admin = True
        session.commit()
        session.refresh(user)
        print(f"promoted {user.email!r} to admin (user_id={user.id}).")
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI must surface every failure mode
        session.rollback()
        print(f"error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()


__all__ = ["add_parser", "main", "run"]


def main(argv: list[str] | None = None) -> int:
    """Module-level entry point used by ``apply_pilot.cli.main`` and tests.

    Builds the sub-command parser, parses *argv*, and delegates to
    :func:`run`. Exits the process with the returned exit code on
    argparse failures (so ``--help`` and missing-required-flag both
    behave like every other well-behaved CLI).
    """
    parser = argparse.ArgumentParser(prog="apply-pilot promote")
    parser.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    return run(args)
