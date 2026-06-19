"""Command-line entry points for ``apply-pilot``.

A thin package that exposes a single ``main`` function (wired through
``[project.scripts]`` in ``pyproject.toml``) and delegates to
sub-command modules. The first sub-command is ``promote`` (issue #171),
which flips the ``is_admin`` flag on an existing user.

The CLI deliberately does not start the FastAPI app — it is a thin
operator tool that opens its own SQLAlchemy session via the same
``apply_pilot.db.SessionLocal`` the app uses, so the operator does not
have to know which database the deployment is wired to.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to the right sub-command. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="apply-pilot",
        description="Operator CLI for the ApplyPilot admin surface.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ``promote`` — flip is_admin=True on an existing user.
    from apply_pilot.cli.promote import add_parser as add_promote_parser

    add_promote_parser(subparsers)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "promote":
        from apply_pilot.cli.promote import run as run_promote

        return run_promote(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
