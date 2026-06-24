"""DTOs for the HH slice.

The credentials, OAuth, and resume-link DTOs have been removed in M10
alongside the corresponding slices. The public HH read surface
(vacancy search) does not need its own DTOs — search returns
:class:`apply_pilot.features.sources.schemas.Vacancy` rows.
"""

from __future__ import annotations

__all__: list[str] = []
