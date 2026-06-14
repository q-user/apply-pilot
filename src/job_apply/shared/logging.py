"""Logging configuration helpers.

The package exposes a single idempotent :func:`configure_logging` helper that
is safe to call from process entry points (FastAPI lifespan, CLI ``main``,
worker ``run``). It deliberately resets the root logger's handlers on every
invocation so that:

* repeated calls (tests, reloads, supervisor restarts) do not stack
  formatters and produce duplicated output, and
* configuration changes from the environment take effect immediately.

Configuration precedence is: explicit keyword arguments override
environment variables, which override the built-in defaults. This mirrors
the pattern used by :mod:`job_apply.config` for ``FastAPISettings``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

# Module-level flag so that re-imports do not re-attach the same handler in
# the common case. ``configure_logging`` still rebuilds handlers on every
# call so that callers can change level/format at runtime; the flag only
# guards against double-invocation during a single import chain.
_CONFIGURED = False

_DEFAULT_LEVEL = "INFO"
_DEFAULT_JSON = True

# Level names that ``logging`` accepts. Kept in sync with the stdlib to fail
# loudly on typos instead of silently falling back to WARNING.
_VALID_LEVELS = {
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
    "NOTSET",
}


def _resolve_level(level: str | None) -> str:
    """Return ``level`` if given, else the ``APP_LOG_LEVEL`` env var or the default."""
    resolved = level if level is not None else os.getenv("APP_LOG_LEVEL", _DEFAULT_LEVEL)
    normalized = resolved.upper()
    if normalized not in _VALID_LEVELS:
        # Failing loud beats silently downgrading to WARNING in a way the
        # operator did not ask for.
        raise ValueError(
            f"Unknown log level: {resolved!r}. Expected one of {sorted(_VALID_LEVELS)}."
        )
    return normalized


def _resolve_json(json_flag: bool | None) -> bool:
    """Return ``json_flag`` if given, else the ``APP_LOG_JSON`` env var or the default."""
    if json_flag is not None:
        return json_flag
    raw = os.getenv("APP_LOG_JSON")
    if raw is None:
        return _DEFAULT_JSON
    return raw.lower() in ("1", "true", "yes", "on")


class _JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.default_time_format),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    *,
    level: str | None = None,
    json: bool | None = None,  # noqa: A001 - matches the public env var name
    stream: Any | None = None,
) -> logging.Logger:
    """Configure the root logger and return it.

    Parameters
    ----------
    level:
        Desired log level (``"DEBUG"``, ``"INFO"``, ...). When ``None`` the
        value of the ``APP_LOG_LEVEL`` environment variable is used.
    json:
        If ``True`` records are emitted as JSON lines, otherwise as
        ``"%(asctime)s [%(levelname)s] %(name)s: %(message)s"``. When
        ``None`` the value of the ``APP_LOG_JSON`` environment variable is
        used.
    stream:
        Optional stream to write to. Defaults to :data:`sys.stderr`. Tests
        may pass a :class:`io.StringIO` to capture output.

    Returns
    -------
    logging.Logger
        The configured root logger. Returning it makes it easy to write
        ``logger = configure_logging(...)`` and then call ``logger.info(...)``
        without a second ``logging.getLogger()`` lookup.
    """
    resolved_level = _resolve_level(level)
    resolved_json = _resolve_json(json)

    root = logging.getLogger()
    # Idempotency: drop every existing handler so repeated calls do not
    # stack formatters. Tests rely on this; production supervisors that
    # restart the process get a clean slate for free.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = (
        logging.StreamHandler(stream=stream) if stream is not None else logging.StreamHandler()
    )
    if resolved_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # Stash the resolution in module state for the rare caller that wants
    # to know which mode was selected.
    global _CONFIGURED
    _CONFIGURED = True
    return root


def is_configured() -> bool:
    """Return ``True`` if :func:`configure_logging` has run at least once in this process."""
    return _CONFIGURED
