"""Daily MAX messenger statistics digest.

The slice mirrors :mod:`apply_pilot.features.telegram.digest` for the
MAX bot channel. It owns the per-user stats aggregation, the
Markdown rendering (re-used from the channel-agnostic
:mod:`apply_pilot.features.messaging.digest.render`), and the scheduled
runner that fires the digest once a day at the configured UTC hour.

The collaborators (:class:`apply_pilot.features.matches.repository.VacancyMatchRepository`,
:class:`apply_pilot.features.max.repository.MaxAccountRepository`,
:class:`apply_pilot.features.users.repository.UsersRepository`) are
injected via the constructor so the slice is exercisable end-to-end
with the in-memory fakes.

Public surface:

* :class:`MaxDigestSender` — sends a digest to one or many users.
* :class:`MaxDigestRunner` — long-running :class:`~apply_pilot.runtime.process.BaseProcess`
  that fires the digest once a day at the configured UTC hour.
* :class:`MaxStatsService` — aggregates counts for a single user and
  enumerates every user with a linked MAX account.
"""

from apply_pilot.features.max.digest.runner import MaxDigestRunner
from apply_pilot.features.max.digest.sender import MaxDigestSender
from apply_pilot.features.max.digest.service import MaxStatsService
from apply_pilot.features.telegram.digest.models import UserStats

__all__ = [
    "MaxDigestRunner",
    "MaxDigestSender",
    "MaxStatsService",
    "UserStats",
]
