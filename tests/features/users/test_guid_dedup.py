"""Regression test for #264: ``GUID`` must have a single source of truth.

The :class:`apply_pilot.features.users.models.GUID` symbol is a re-export of
:class:`apply_pilot.shared.types.GUID`. Anything that re-imports it through
``users.models`` must end up referencing the exact same class object so the
two definitions cannot drift apart.
"""

from __future__ import annotations

from apply_pilot.features.users.models import GUID as UsersGUID
from apply_pilot.shared.types import GUID as SharedGUID


def test_users_models_guid_is_shared_types_guid() -> None:
    """The class imported via ``users.models`` IS the class in ``shared.types``."""
    assert UsersGUID is SharedGUID
