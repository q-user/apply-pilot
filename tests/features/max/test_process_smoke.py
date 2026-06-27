"""Subprocess smoke tests for the ``apply-pilot-max-bot`` console script.

These tests boot the actual entry point in a fresh subprocess so a regression
that breaks the script's import graph (e.g. the ``matches`` ↔
``apply_worker`` cycle fixed in #229) or the packaging pipeline (e.g. the
console-script entry missing from the wheel — the historical
``executable file not found in $PATH`` failure mode) cannot slip through
the unit-test suite unnoticed.

The smoke is intentionally shallow: the fake token gets a 401 from
``botapi.max.ru`` and the bot logs a ``max.getUpdates.failed`` event before
backing off. We do **not** assert on that 401 — it is the responsibility of
``_ERROR_BACKOFF_SECONDS`` and the structured-log contract. We only assert
that ``process.start`` is emitted (proving the script reached steady state)
and that no fatal error / import problem shows up in the captured streams.

The tests are POSIX-only because they rely on ``SIGTERM``/``SIGKILL`` for
clean teardown.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# Repo root = tests/features/max/test_process_smoke.py → ../../../
REPO_ROOT = Path(__file__).resolve().parents[3]
CONSOLE_SCRIPT = "apply-pilot-max-bot"

# Strings that, when observed in the captured output, indicate the bot
# failed to reach steady state. Kept as a tuple constant so both the
# import-time and the runtime assertions share the same allowlist.
_FATAL_FRAGMENTS: tuple[str, ...] = (
    "ImportError",
    "circular import",
    "PartiallyInitializedModuleError",
    "executable file not found",
    "Traceback",
    "ModuleNotFoundError",
    "SyntaxError",
)

# Default wall-clock budget for a smoke run. 5 s is comfortably above the
# ~0.5 s it takes for ``process.start`` to appear on a warm venv and below
# the project-wide ``pytest-timeout = 5`` ceiling, so the test function
# itself never trips the global timeout when it terminates early on
# signal.
_DEFAULT_TIMEOUT = 5.0


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signals only; SIGTERM/SIGKILL teardown is not portable",
)


@dataclass
class _BotRun:
    """In-flight bot subprocess with thread-safe captured output.

    The reader threads append to ``stdout_chunks`` / ``stderr_chunks``
    under a single lock; ``snapshot()`` returns the current joined view
    without forcing the test to synchronize. ``terminate_and_wait`` is
    idempotent: a second call is a no-op.
    """

    proc: subprocess.Popen[str]
    cwd: Path
    timeout: float
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stdout_chunks: list[str] = field(default_factory=list)
    _stderr_chunks: list[str] = field(default_factory=list)
    _terminated: bool = False
    returncode: int | None = None

    def snapshot(self) -> tuple[str, str]:
        """Return the current ``(stdout, stderr)`` joined view.

        Cheap enough to call in a tight poll loop.
        """
        with self._lock:
            return "".join(self._stdout_chunks), "".join(self._stderr_chunks)

    def _append(self, stream: list[str], chunk: str) -> None:
        with self._lock:
            stream.append(chunk)

    def terminate_and_wait(self) -> int:
        """Stop the subprocess and drain its output.

        Sends ``SIGTERM`` first to give the asyncio loop a chance to log
        a clean ``process.shutdown`` event, then escalates to ``SIGKILL``
        if the process does not exit within 2 s. Returns the final
        ``returncode`` (``-SIGTERM`` is acceptable — the bot is not
        expected to shut down deterministically from a signal under test).
        """
        if self._terminated:
            return self.returncode if self.returncode is not None else -1
        self._terminated = True

        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.returncode = self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.returncode = self.proc.wait(timeout=2.0)
        else:
            self.returncode = self.proc.returncode

        # Give the reader threads a moment to drain whatever is left in
        # the pipes. They will exit on their own once the pipes close.
        for thread in (self._stdout_thread, self._stderr_thread):
            thread.join(timeout=1.0)
        return self.returncode

    _stdout_thread: threading.Thread = field(init=False)
    _stderr_thread: threading.Thread = field(init=False)


def _stream_reader(stream: Iterable[str], sink: list[str], lock: threading.Lock) -> None:
    """Drain ``stream`` line-by-line into ``sink`` under ``lock``.

    Iterating on the file object directly (rather than calling ``.read``)
    yields complete lines as they are flushed, which pairs well with
    ``PYTHONUNBUFFERED=1`` in the bot's environment.
    """
    for line in stream:
        with lock:
            sink.append(line)


def _run_bot(env: dict[str, str], *, timeout: float = _DEFAULT_TIMEOUT) -> _BotRun:
    """Boot the ``apply-pilot-max-bot`` console script in a subprocess.

    ``env`` is merged on top of ``os.environ`` so callers only need to
    specify the variables they care about. Output is captured by two
    daemon reader threads and exposed via :meth:`_BotRun.snapshot`.
    The caller is responsible for calling
    :meth:`_BotRun.terminate_and_wait` once it has seen what it needs
    (or wants to give up after ``timeout`` seconds).
    """
    # Resolve the executable inside the worktree venv. ``shutil.which``
    # would also work, but spelling out the path makes the test
    # independent of the developer shell's ``$PATH`` (uv venv binaries
    # are not on ``$PATH`` by default).
    venv_bin = REPO_ROOT / ".venv" / "bin" / CONSOLE_SCRIPT
    if venv_bin.exists():
        executable = str(venv_bin)
    else:
        which = shutil.which(CONSOLE_SCRIPT)
        if which is None:
            raise FileNotFoundError(f"{CONSOLE_SCRIPT} not found in .venv/bin/ nor on $PATH")
        executable = which

    merged_env = {**os.environ, **env}

    proc = subprocess.Popen(  # noqa: S603 — controlled test subprocess
        [executable],
        cwd=str(REPO_ROOT),
        env=merged_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered; relies on PYTHONUNBUFFERED too
    )

    run = _BotRun(proc=proc, cwd=REPO_ROOT, timeout=timeout)
    run._stdout_thread = threading.Thread(  # noqa: SLF001 — internal wiring
        target=_stream_reader,
        args=(proc.stdout, run._stdout_chunks, run._lock),  # noqa: SLF001
        name="max-bot-stdout",
        daemon=True,
    )
    run._stderr_thread = threading.Thread(  # noqa: SLF001
        target=_stream_reader,
        args=(proc.stderr, run._stderr_chunks, run._lock),  # noqa: SLF001
        name="max-bot-stderr",
        daemon=True,
    )
    run._stdout_thread.start()
    run._stderr_thread.start()
    return run


def _wait_for_signal(
    run: _BotRun,
    *,
    needle: str,
    timeout: float,
    poll_interval: float = 0.05,
) -> bool:
    """Return ``True`` as soon as ``needle`` appears in either stream.

    The function never blocks longer than ``timeout``; on timeout it
    returns ``False`` so the test can assert on the absence of the
    signal cleanly.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stdout, stderr = run.snapshot()
        if needle in stdout or needle in stderr:
            return True
        if run.proc.poll() is not None:
            # Process exited on its own — give the readers one more
            # snapshot to flush whatever they had buffered.
            time.sleep(poll_interval)
            stdout, stderr = run.snapshot()
            return needle in stdout or needle in stderr
        time.sleep(poll_interval)
    return False


def test_console_script_exists() -> None:
    """The ``apply-pilot-max-bot`` console script is installed in the venv.

    Regression guard for the historical ``executable file not found in
    $PATH`` failure mode (typically caused by a broken ``[project.scripts]``
    block in ``pyproject.toml`` or a Dockerfile that does not run
    ``uv sync`` / ``pip install`` before the entry point is needed).
    """
    venv_bin = REPO_ROOT / ".venv" / "bin" / CONSOLE_SCRIPT
    assert venv_bin.exists() or shutil.which(CONSOLE_SCRIPT) is not None, (
        f"{CONSOLE_SCRIPT} must be installed in the worktree venv or on $PATH; "
        f"looked at {venv_bin} and $PATH={os.environ.get('PATH', '')!r}"
    )


def test_boot_with_fake_token_does_not_crash_on_imports() -> None:
    """The bot reaches ``process.start`` and never blows up on import.

    A fake ``MAX_BOT_TOKEN`` causes the MAX API to return 401 on the
    first ``getUpdates`` call. That is the expected runtime failure
    mode; the structured log line is the bot's job to emit. This test
    only verifies that the import graph is healthy and the polling
    loop is actually running.
    """
    run = _run_bot(
        env={
            "MAX_BOT_TOKEN": "smoke-test-token",
            "MAX_POLLING_TIMEOUT": "2",
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "PYTHONUNBUFFERED": "1",
        },
    )
    try:
        # ``process.start`` is logged synchronously by ``BaseProcess.start``
        # before the first ``await get_updates``, so it appears within
        # ~0.5 s of a healthy boot. 4 s of budget is plenty.
        assert _wait_for_signal(run, needle="process.start", timeout=4.0), (
            "process.start did not appear within 4 s; bot likely failed "
            "to import or the polling loop never entered.\n"
            f"stdout={run.snapshot()[0]!r}\nstderr={run.snapshot()[1]!r}"
        )

        stdout, stderr = run.snapshot()
        combined = stdout + stderr
        for fragment in _FATAL_FRAGMENTS:
            assert fragment not in combined, (
                f"unexpected {fragment!r} in bot output — the import graph "
                f"or packaging is broken again.\nstdout={stdout!r}\nstderr={stderr!r}"
            )
    finally:
        run.terminate_and_wait()


def test_missing_token_exits_cleanly() -> None:
    """An empty ``MAX_BOT_TOKEN`` triggers the fail-fast ``ValueError``.

    ``process.main()`` reads the settings eagerly and raises before
    importing any of the action-handler modules, so the bot must
    exit non-zero with the canonical message and never reach the
    polling loop.
    """
    run = _run_bot(
        env={
            "MAX_BOT_TOKEN": "",
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        },
    )
    try:
        try:
            returncode = run.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            run.terminate_and_wait()
            pytest.fail(
                "bot did not exit within 3 s with an empty MAX_BOT_TOKEN; "
                "the fail-fast ValueError is supposed to surface immediately.\n"
                f"stdout={run.snapshot()[0]!r}\nstderr={run.snapshot()[1]!r}"
            )

        assert returncode != 0, "bot exited cleanly with rc=0 even though MAX_BOT_TOKEN was empty"

        stdout, stderr = run.snapshot()
        combined = stdout + stderr
        assert "ValueError: MAX_BOT_TOKEN environment variable must be set" in combined, (
            "expected the canonical MAX_BOT_TOKEN ValueError in stdout/stderr; "
            f"got stdout={stdout!r}, stderr={stderr!r}"
        )
        # And the process must not have emitted the steady-state start
        # event before failing — that would mean the import-order
        # guarantee in ``process.main()`` regressed.
        assert "process.start" not in combined, (
            "process.start was emitted before the MAX_BOT_TOKEN check ran; "
            "the fail-fast guard is no longer ahead of the unsafe imports"
        )
    finally:
        run.terminate_and_wait()
