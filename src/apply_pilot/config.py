"""Application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apply_pilot.features.apply_worker.retry import RetryPolicy


@dataclass(frozen=True)
class Settings:
    database_url: str

    # --- Redis runtime (M0, issue #8) ---
    # Defaults keep the dataclass drop-in backwards compatible for any
    # call site that only constructs ``Settings(database_url=...)``.
    redis_url: str = "redis://localhost:6379/0"
    redis_db: int = 0
    redis_password: str | None = None


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite+pysqlite:///./app.db"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_db=int(os.getenv("REDIS_DB", "0")),
        redis_password=os.getenv("REDIS_PASSWORD") or None,
    )


@dataclass(frozen=True)
class FastAPISettings:
    app_name: str
    host: str
    port: int
    log_level: str
    log_json: bool


def get_fastapi_settings() -> FastAPISettings:
    return FastAPISettings(
        app_name=os.getenv("APP_NAME", "apply-pilot"),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
        log_json=os.getenv("APP_LOG_JSON", "true").lower() in ("1", "true", "yes", "on"),
    )


@dataclass(frozen=True)
class DatabaseSettings:
    """Database connection settings (SQLAlchemy-oriented).

    Read `DATABASE_URL` from the environment when present; otherwise fall back
    to a local sqlite file suitable for development.
    """

    database_url: str = "sqlite:///./dev.db"
    pool_size: int = 5
    max_overflow: int = 10
    pool_pre_ping: bool = True
    echo: bool = False


def get_database_settings() -> DatabaseSettings:
    """Build DatabaseSettings from the environment, honoring DATABASE_URL."""
    return DatabaseSettings(database_url=os.getenv("DATABASE_URL", "sqlite:///./dev.db"))


@dataclass(frozen=True)
class TelegramSettings:
    """Telegram bot configuration (M1, issue #14).

    Attributes:
        bot_token: The bot token issued by `@BotFather`. There is no
            default — the entry point (``apply-pilot-bot``) refuses to
            start without it because an empty token would silently
            produce 404s against ``api.telegram.org``.
        polling_timeout: Long-poll timeout (seconds) for ``getUpdates``.
            Defaults to 30 seconds, which matches the recommended
            maximum for Telegram long polling.

    Environment variables:

    * ``TELEGRAM_BOT_TOKEN`` (required)
    * ``TELEGRAM_POLLING_TIMEOUT`` (optional, default ``30``)
    """

    bot_token: str
    polling_timeout: int = 30

    def __post_init__(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "TelegramSettings.bot_token must be a non-empty string; "
                "set the TELEGRAM_BOT_TOKEN environment variable."
            )
        if self.polling_timeout <= 0:
            raise ValueError("TelegramSettings.polling_timeout must be a positive integer")


def get_telegram_settings() -> TelegramSettings:
    """Build TelegramSettings from the environment.

    Raises:
        ValueError: If ``TELEGRAM_BOT_TOKEN`` is unset or empty. The
            check is intentionally eager so misconfiguration surfaces
            at process start, not at the first failed HTTP call.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN environment variable must be set to a non-empty value."
        )
    raw_timeout = os.getenv("TELEGRAM_POLLING_TIMEOUT", "30")
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            f"TELEGRAM_POLLING_TIMEOUT must be an integer (got {raw_timeout!r})."
        ) from exc
    return TelegramSettings(bot_token=token, polling_timeout=timeout)


@dataclass(frozen=True)
class MaxSettings:
    """MAX bot configuration (M9, issue #176).

    Attributes:
        bot_token: The bot token issued by the MAX web UI. There is no
            default — the entry point refuses to start without it because
            an empty token would silently produce auth failures against
            ``botapi.max.ru``.
        polling_timeout: Long-poll timeout (seconds) for ``getUpdates``.
            Defaults to 30 seconds. MUST be in the range 0-90 per the MAX
            API contract.
        api_base: Base URL for the MAX Bot API. Override only for testing
            against a mock server.

    Environment variables:

    * ``MAX_BOT_TOKEN`` (required)
    * ``MAX_POLLING_TIMEOUT`` (optional, default ``30``)
    * ``MAX_API_BASE`` (optional, default ``https://botapi.max.ru``)
    """

    bot_token: str
    polling_timeout: int = 30
    api_base: str = "https://botapi.max.ru"

    def __post_init__(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "MaxSettings.bot_token must be a non-empty string; "
                "set the MAX_BOT_TOKEN environment variable."
            )
        if self.polling_timeout <= 0:
            raise ValueError("MaxSettings.polling_timeout must be a positive integer")


def get_max_settings() -> MaxSettings:
    """Build MaxSettings from the environment.

    Raises:
        ValueError: If ``MAX_BOT_TOKEN`` is unset or empty. The check is
            intentionally eager so misconfiguration surfaces at process
            start, not at the first failed HTTP call.
    """
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("MAX_BOT_TOKEN environment variable must be set to a non-empty value.")
    raw_timeout = os.getenv("MAX_POLLING_TIMEOUT", "30")
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise ValueError(f"MAX_POLLING_TIMEOUT must be an integer (got {raw_timeout!r}).") from exc
    api_base = os.getenv("MAX_API_BASE", "https://botapi.max.ru").strip() or "https://botapi.max.ru"
    return MaxSettings(bot_token=token, polling_timeout=timeout, api_base=api_base)


# ---------------------------------------------------------------------------
# Auth settings (M1, issue #11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthSettings:
    """Auth-slice settings.

    Kept as a separate, frozen dataclass so the FastAPI app can wire it
    up independently of the database/Redis bundles and so tests can
    override individual fields (notably the token TTL) without touching
    the rest of the configuration surface.

    Environment variables:

    * ``APP_AUTH_TOKEN_TTL_SECONDS`` — how long an issued bearer token
      remains valid. Defaults to 8 hours.
    * ``APP_AUTH_PBKDF2_ITERATIONS`` — PBKDF2 work factor. Defaults to
      200_000 per the OWASP 2023 recommendation for PBKDF2-SHA256.
    """

    token_ttl_seconds: int = 60 * 60 * 8
    pbkdf2_iterations: int = 200_000


def get_auth_settings() -> AuthSettings:
    """Build :class:`AuthSettings` from the environment, applying defaults."""
    return AuthSettings(
        token_ttl_seconds=int(os.getenv("APP_AUTH_TOKEN_TTL_SECONDS", str(60 * 60 * 8))),
        pbkdf2_iterations=int(os.getenv("APP_AUTH_PBKDF2_ITERATIONS", "200000")),
    )


# --- Admin settings (M6, issue #145) ----------------------------------------


def get_admin_auth_required() -> bool:
    """Read ``APP_ADMIN_REQUIRE_AUTH`` from the environment.

    Returns ``True`` (auth required) when the variable is unset or set to
    a truthy value (``1``/``true``/``yes``/``on``). Returns ``False``
    only when the operator explicitly opts out (``0``/``false``/``no``/``off``).
    Unparseable values raise :class:`ValueError` at startup so a typo
    surfaces immediately.

    The default is ``True`` so production deployments that forget to
    set the flag do not silently expose unauthenticated admin endpoints
    (issue #145).
    """
    raw = os.getenv("APP_ADMIN_REQUIRE_AUTH", "true")
    return _parse_bool(raw, env_var="APP_ADMIN_REQUIRE_AUTH")


# --- Resume settings (M1, issues #15 and #16) -------------------------------
_RESUMES_DEFAULT_MAX_FILE_SIZE_MB = 10


@dataclass(frozen=True)
class ResumeSettings:
    """Configuration for the resumes vertical slice."""

    max_file_size_mb: int = _RESUMES_DEFAULT_MAX_FILE_SIZE_MB

    @property
    def max_file_size_bytes(self) -> int:
        """Maximum permitted upload size, in bytes."""
        return self.max_file_size_mb * 1024 * 1024


def get_resume_settings() -> ResumeSettings:
    """Build ResumeSettings from the environment, honoring APP_RESUMES_MAX_FILE_SIZE_MB."""
    raw = os.getenv("APP_RESUMES_MAX_FILE_SIZE_MB", str(_RESUMES_DEFAULT_MAX_FILE_SIZE_MB))
    try:
        max_mb = int(raw)
    except ValueError as exc:
        raise ValueError(f"APP_RESUMES_MAX_FILE_SIZE_MB must be an integer; got {raw!r}") from exc
    if max_mb <= 0:
        raise ValueError(f"APP_RESUMES_MAX_FILE_SIZE_MB must be a positive integer; got {max_mb}")
    return ResumeSettings(max_file_size_mb=max_mb)


# --- Digest settings (M4, issue #35) ----------------------------------------
_DIGEST_DEFAULT_HOUR_UTC = 9


@dataclass(frozen=True)
class DigestSettings:
    """Configuration for the daily Telegram digest slice.

    Attributes:
        digest_hour_utc: Hour of the day (UTC, 0-23) at which the
            digest runner fires. Defaults to 9 to match the documented
            behaviour ("daily Telegram statistics digest" sent in the
            morning UTC).

    Environment variables:

    * ``APP_DIGEST_HOUR_UTC`` (optional, default ``9``) — see
      :attr:`digest_hour_utc`.
    """

    digest_hour_utc: int = _DIGEST_DEFAULT_HOUR_UTC

    def __post_init__(self) -> None:
        if not 0 <= self.digest_hour_utc <= 23:
            raise ValueError(
                f"DigestSettings.digest_hour_utc must be in [0, 23]; got {self.digest_hour_utc}"
            )


def get_digest_settings() -> DigestSettings:
    """Build :class:`DigestSettings` from the environment.

    Surfaces a clear error when the configured hour is out of range
    so a typo (e.g. ``24``) fails fast at process start.
    """
    raw = os.getenv("APP_DIGEST_HOUR_UTC", str(_DIGEST_DEFAULT_HOUR_UTC))
    try:
        hour = int(raw)
    except ValueError as exc:
        raise ValueError(f"APP_DIGEST_HOUR_UTC must be an integer; got {raw!r}") from exc
    return DigestSettings(digest_hour_utc=hour)


# --- Apply worker settings (M5, issue #47) ---------------------------------


@dataclass(frozen=True)
class ApplyWorkerSettings:
    """Configuration for the apply-worker retry policy (M5, issue #47).

    The settings are loaded from environment variables at process
    start. ``get_apply_worker_settings()`` is the only entry point; it
    raises :class:`ValueError` on any malformed value so misconfiguration
    surfaces at boot time, not at the first failed apply job.

    Environment variables (all optional; defaults shown):

    * ``APP_APPLY_MAX_ATTEMPTS`` (int, default ``3``) — maximum number
      of attempts before a job is dead-lettered.
    * ``APP_APPLY_BASE_DELAY_SECONDS`` (float, default ``2.0``) —
      delay applied after the first attempt.
    * ``APP_APPLY_MAX_DELAY_SECONDS`` (float, default ``300.0``) — cap
      on the backoff delay.
    * ``APP_APPLY_BACKOFF_MULTIPLIER`` (float, default ``2.0``) —
      geometric growth factor between attempts.
    * ``APP_APPLY_JITTER`` (bool, default ``true``) — whether to
      perturb each delay with ±10% jitter. Accepted truthy values
      match the convention used by :class:`FastAPISettings`:
      ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    * ``APP_APPLY_HOURLY_LIMIT`` (int, default ``10``) — maximum
      number of :meth:`ApplyJobService.enqueue_for_match` calls per
      user per rolling 1-hour window. M5, issue #46.
    * ``APP_APPLY_DAILY_LIMIT`` (int, default ``30``) — maximum number
      of enqueue calls per user per rolling 24-hour window. M5, issue
      #46.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    jitter: bool = True
    hourly_limit: int = 10
    daily_limit: int = 30

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                f"ApplyWorkerSettings.max_attempts must be a positive integer; "
                f"got {self.max_attempts}"
            )
        if self.base_delay_seconds <= 0:
            raise ValueError(
                f"ApplyWorkerSettings.base_delay_seconds must be positive; "
                f"got {self.base_delay_seconds}"
            )
        if self.max_delay_seconds <= 0:
            raise ValueError(
                f"ApplyWorkerSettings.max_delay_seconds must be positive; "
                f"got {self.max_delay_seconds}"
            )
        if self.backoff_multiplier <= 0:
            raise ValueError(
                f"ApplyWorkerSettings.backoff_multiplier must be positive; "
                f"got {self.backoff_multiplier}"
            )
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "ApplyWorkerSettings.max_delay_seconds must be >= base_delay_seconds; "
                f"got max={self.max_delay_seconds}, base={self.base_delay_seconds}"
            )
        if self.hourly_limit < 1:
            raise ValueError(
                f"ApplyWorkerSettings.hourly_limit must be a positive integer; "
                f"got {self.hourly_limit}"
            )
        if self.daily_limit < 1:
            raise ValueError(
                f"ApplyWorkerSettings.daily_limit must be a positive integer; "
                f"got {self.daily_limit}"
            )

    def to_retry_policy(self) -> RetryPolicy:
        """Return a :class:`~apply_pilot.features.apply_worker.retry.RetryPolicy`.

        The :class:`RetryPolicy` is imported lazily inside the method
        to avoid the circular dependency chain
        ``db -> config -> apply_worker.retry -> apply_worker.__init__ ->
        apply_worker.models -> db`` that would otherwise fire on
        ``apply_pilot.config`` import. ``from __future__ import
        annotations`` keeps the type annotation a bare string, so
        static type checkers resolve it through the local alias and
        runtime never touches the import.
        """
        from apply_pilot.features.apply_worker.retry import RetryPolicy

        return RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay_seconds=self.base_delay_seconds,
            max_delay_seconds=self.max_delay_seconds,
            backoff_multiplier=self.backoff_multiplier,
            jitter=self.jitter,
        )


def _parse_bool(value: str, *, env_var: str) -> bool:
    """Parse a boolean from an env-var string with explicit error messages."""
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"{env_var} must be a boolean (1/true/yes/on or 0/false/no/off); got {value!r}"
    )


def _parse_positive_int(value: str, *, env_var: str) -> int:
    """Parse a positive int from an env-var string."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{env_var} must be an integer; got {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{env_var} must be a positive integer; got {parsed}")
    return parsed


def _parse_positive_float(value: str, *, env_var: str) -> float:
    """Parse a positive float from an env-var string."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{env_var} must be a float; got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{env_var} must be a positive float; got {parsed}")
    return parsed


def get_apply_worker_settings() -> ApplyWorkerSettings:
    """Build :class:`ApplyWorkerSettings` from the environment.

    All five knobs are optional; the dataclass defaults match the
    operational defaults documented in the M5 spec. Errors are raised
    eagerly so a typo (``APP_APPLY_MAX_ATTEMPTS=three``) surfaces at
    process start, not at the first failed retry.
    """
    max_attempts_raw = os.getenv("APP_APPLY_MAX_ATTEMPTS", "3")
    base_delay_raw = os.getenv("APP_APPLY_BASE_DELAY_SECONDS", "2.0")
    max_delay_raw = os.getenv("APP_APPLY_MAX_DELAY_SECONDS", "300.0")
    multiplier_raw = os.getenv("APP_APPLY_BACKOFF_MULTIPLIER", "2.0")
    jitter_raw = os.getenv("APP_APPLY_JITTER", "true")
    hourly_raw = os.getenv("APP_APPLY_HOURLY_LIMIT", "10")
    daily_raw = os.getenv("APP_APPLY_DAILY_LIMIT", "30")

    return ApplyWorkerSettings(
        max_attempts=_parse_positive_int(max_attempts_raw, env_var="APP_APPLY_MAX_ATTEMPTS"),
        base_delay_seconds=_parse_positive_float(
            base_delay_raw, env_var="APP_APPLY_BASE_DELAY_SECONDS"
        ),
        max_delay_seconds=_parse_positive_float(
            max_delay_raw, env_var="APP_APPLY_MAX_DELAY_SECONDS"
        ),
        backoff_multiplier=_parse_positive_float(
            multiplier_raw, env_var="APP_APPLY_BACKOFF_MULTIPLIER"
        ),
        jitter=_parse_bool(jitter_raw, env_var="APP_APPLY_JITTER"),
        hourly_limit=_parse_positive_int(hourly_raw, env_var="APP_APPLY_HOURLY_LIMIT"),
        daily_limit=_parse_positive_int(daily_raw, env_var="APP_APPLY_DAILY_LIMIT"),
    )


# --- Careers-page settings (M7, issue #59) ---------------------------------


def get_careers_page_config():
    """Build the :class:`CareersPageConfig` from the environment.

    The list of configured sites is read from a single JSON-encoded
    env var (``APP_CAREERS_PAGES``). An empty / unset value is
    treated as "no sites configured" — the careers adapter registry
    is then empty and the careers feature is effectively disabled.

    Environment variables:

    * ``APP_CAREERS_PAGES`` (optional) — JSON list of site entries
      (see :class:`~apply_pilot.features.careers.config.CareersPageSite`).

    The import is local to avoid a cycle (``apply_pilot.config`` is
    imported very early in the boot sequence; the careers slice
    transitively imports the screening ORM models).
    """
    from apply_pilot.features.careers.config import CareersPageConfig

    return CareersPageConfig.from_json(os.getenv("APP_CAREERS_PAGES", ""))
