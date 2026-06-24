"""Subprocess smoke tests for the ``apply-pilot-max-bot`` console script.

These tests boot the actual console script as a child process and assert
that:

* the entry point is installed in the venv (catches Dockerfile /
  ``[project.scripts]`` regressions that surface as
  ``executable file not found in $PATH``),
* the script does not crash in the first few seconds with an
  ``ImportError`` (catches the ``apply_worker`` ↔ ``matches`` circular
  import re-introduced by #220),
* the fail-fast path in :func:`apply_pilot.features.max.process.main`
  raises ``ValueError`` when ``MAX_BOT_TOKEN`` is empty.

The fake token (``smoke-test-token``) is intentionally invalid — the
test does not care about the resulting 401 from the MAX API, only that
the process gets that far. The structured ``process.start`` log line is
emitted by :meth:`BaseProcess.start` BEFORE the first ``getUpdates``,
so a 4–5 s observation window is plenty.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# The script lives in tests/features/max/, so the repo root is three
# levels up — same convention as the alembic migration smoke tests.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Cycle that currently blocks the bot from booting. The fix lands in
# the import-graph-guard work (issue #221). Once that PR is merged
# the ``xfail`` below turns into an XPASS and the strict marker
# promotes it to a regular pass.
_CIRCULAR_IMPORT_REASON = (
    "apply_worker ↔ matches import cycle (issue #221) still blocks bot boot; "
    "strict xfail flips to pass once the cycle is broken at the source"
)


def _spawn_bot(env: dict[str, str]) -> subprocess.Popen[str]:
    """Launch ``uv run apply-pilot-max-bot`` as a child process.

    Uses ``text=True`` + ``bufsize=1`` so the pipes are line-buffered
    text streams that drain cleanly in background threads (the OS
    pipe buffer is small; a bot that logs every event would block
    on a full pipe within a second).
    """
    merged_env = {**os.environ, **env}
    return subprocess.Popen(
        ["uv", "run", "apply-pilot-max-bot"],
        cwd=REPO_ROOT,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _drain_for(proc: subprocess.Popen[str], seconds: float) -> tuple[str, str]:
    """Read ``proc``'s stdout/stderr for ``seconds`` seconds.

    Spawns one reader thread per stream so the OS pipe buffer can
    never fill up and stall the child. Returns the concatenated
    captured output. The process is NOT terminated here — callers
    decide whether to kill, wait, or let it run.
    """
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _drain(stream: object, sink: list[str]) -> None:
        assert hasattr(stream, "readline")
        for line in iter(stream.readline, ""):  # type: ignore[attr-defined]
            sink.append(line)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            # Child exited; give the readers a moment to flush.
            time.sleep(0.1)
            break
        time.sleep(0.05)

    return "".join(stdout_chunks), "".join(stderr_chunks)


def _terminate(proc: subprocess.Popen[str], *, grace: float = 2.0) -> None:
    """SIGTERM the child, escalate to SIGKILL if it ignores the request."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=grace)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_console_script_exists() -> None:
    """The ``apply-pilot-max-bot`` entry point is installed in the venv.

    A bare ``uv run`` on an uninstalled project raises
    ``executable file not found in $PATH``; that is the exact symptom
    of the Dockerfile regression this smoke suite exists to catch.
    """
    on_path = shutil.which("apply-pilot-max-bot")
    in_venv = REPO_ROOT / ".venv" / "bin" / "apply-pilot-max-bot"
    assert in_venv.exists(), (
        f"console script missing at {in_venv}; "
        "check pyproject [project.scripts] and Dockerfile runtime stage"
    )
    # ``shutil.which`` should resolve it via the venv once uv is on PATH;
    # we don't require it (CI runners may not have the venv activated)
    # but we capture it for triage.
    _ = on_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
@pytest.mark.xfail(reason=_CIRCULAR_IMPORT_REASON, strict=True, raises=AssertionError)
def test_boot_with_fake_token_does_not_crash_on_imports() -> None:
    """Boot the bot with a fake token; expect ``process.start`` and no
    ``ImportError`` / ``circular import`` / ``Traceback`` in the first 5 s.

    The fake token will eventually yield a 401 from the MAX API; that
    is expected and not part of this assertion. The window of 4.5 s
    is sized so that :meth:`BaseProcess.start` has already logged
    ``process.start`` and the loop has hit ``getUpdates`` at least once.

    Marked ``xfail(strict=True)`` because the ``apply_worker`` ↔
    ``matches`` circular import (issue #221) currently blocks the
    bot from importing :class:`MaxBot`. Once that fix lands the test
    becomes a hard pass; if it ever silently regresses the strict
    marker promotes the xfail back to a failure.
    """
    proc = _spawn_bot(
        {
            "MAX_BOT_TOKEN": "smoke-test-token",
            "MAX_POLLING_TIMEOUT": "2",
            "APP_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        }
    )
    try:
        out, err = _drain_for(proc, seconds=4.5)
    finally:
        _terminate(proc, grace=2.0)

    combined = out + err

    # Positive signal: the process is alive enough to log its start.
    assert "process.start" in combined, (
        f"expected 'process.start' in output within 4.5s; got:\n{combined}"
    )

    # Negative signals: any of these would mean a regression of #220
    # or a new import cycle in the boot path.
    forbidden_substrings = (
        "ImportError",
        "circular import",
        "PartiallyInitializedModule",
        "executable file not found",
        "Traceback",
    )
    for needle in forbidden_substrings:
        assert needle not in combined, f"forbidden substring {needle!r} in bot output:\n{combined}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_missing_token_exits_cleanly() -> None:
    """An empty ``MAX_BOT_TOKEN`` must trip the fail-fast path.

    :func:`apply_pilot.features.max.process.main` calls
    ``get_max_settings()`` BEFORE pulling in the modules that close
    the ``apply_worker`` ↔ ``matches`` cycle, so a misconfiguration
    surfaces as ``ValueError: MAX_BOT_TOKEN environment variable must
    be set`` rather than a confusing ``ImportError``. The script then
    exits with a non-zero status within a couple of seconds.
    """
    proc = _spawn_bot(
        {
            "MAX_BOT_TOKEN": "",
            "APP_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        }
    )
    try:
        try:
            rc = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
            pytest.fail("bot did not exit within 2s of empty MAX_BOT_TOKEN")
    finally:
        if proc.poll() is None:
            _terminate(proc, grace=2.0)

    # Drain whatever output the child emitted before exiting.
    out = proc.stdout.read() if proc.stdout else ""
    err = proc.stderr.read() if proc.stderr else ""
    combined = out + err

    assert rc != 0, (
        f"expected non-zero exit on empty MAX_BOT_TOKEN, got rc={rc}; output:\n{combined}"
    )
    assert "MAX_BOT_TOKEN" in combined, (
        f"expected 'MAX_BOT_TOKEN' in error output, got:\n{combined}"
    )
