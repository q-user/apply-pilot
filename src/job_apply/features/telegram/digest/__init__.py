"""Daily Telegram statistics digest.

The slice owns the per-user stats aggregation, the Markdown rendering
and the scheduled runner that fires the digest once a day. The
collaborators (``VacancyMatchRepository``,
``TelegramAccountRepository``, ``UsersRepository``) are injected via the
constructor so the slice is exercisable end-to-end with the in-memory
fakes.

Public surface:

* :class:`UserStats` — value object the renderer and sender consume.
* :func:`render_digest_message` — pure function that turns a
  :class:`UserStats` into a Markdown string.
* :class:`StatsService` — aggregates counts for a single user.
* :class:`DigestSender` — sends a digest to one or many users.
* :class:`DigestRunner` — long-running :class:`BaseProcess` that fires
  the digest once a day at the configured UTC hour.
"""

from job_apply.features.telegram.digest.models import UserStats
from job_apply.features.telegram.digest.render import render_digest_message
from job_apply.features.telegram.digest.runner import DigestRunner
from job_apply.features.telegram.digest.sender import DigestSender
from job_apply.features.telegram.digest.service import StatsService

__all__ = [
    "DigestRunner",
    "DigestSender",
    "StatsService",
    "UserStats",
    "render_digest_message",
]
