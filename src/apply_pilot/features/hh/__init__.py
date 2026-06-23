"""HH vertical slice — public read surface only.

The HH OAuth flow and apply-via-API have been removed in M10. Apply
is delegated to a separate headless-browser tool (see issue #206).

This module re-exports the public HH read surface (vacancy search,
source adapter) so other slices can depend on the public names without
coupling to the internal module structure.
"""

from __future__ import annotations

from apply_pilot.features.hh.adapter import HhSourceAdapter as HhSourceAdapter
from apply_pilot.features.hh.search import HhHttpVacancySearchClient as HhHttpVacancySearchClient
from apply_pilot.features.hh.search import HHQuery as HHQuery
from apply_pilot.features.hh.search import HHRateLimitError as HHRateLimitError
from apply_pilot.features.hh.search import HHVacancyNotFoundError as HHVacancyNotFoundError
from apply_pilot.features.hh.search import HHVacancySearchClient as HHVacancySearchClient
from apply_pilot.features.hh.search import HHVacancySearchError as HHVacancySearchError
from apply_pilot.features.hh.search import (
    InMemoryHhVacancySearchClient as InMemoryHhVacancySearchClient,
)

__all__ = [
    "HHQuery",
    "HHRateLimitError",
    "HHVacancyNotFoundError",
    "HHVacancySearchClient",
    "HHVacancySearchError",
    "HhHttpVacancySearchClient",
    "HhSourceAdapter",
    "InMemoryHhVacancySearchClient",
]
