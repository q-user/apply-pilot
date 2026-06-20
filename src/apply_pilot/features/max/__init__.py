"""MAX messenger bot feature slice.

Data layer for the MAX bot integration tracked under the M9 milestone.
Owns the :class:`MaxAccount` ORM and the two repository
implementations; cross-slice dependencies stay on the channel-agnostic
``messaging`` protocols.
"""

from apply_pilot.features.max.models import MaxAccount
from apply_pilot.features.max.repository import (
    InMemoryMaxAccountRepository,
    MaxAccountRepository,
    SqlAlchemyMaxAccountRepository,
)

__all__ = [
    "InMemoryMaxAccountRepository",
    "MaxAccount",
    "MaxAccountRepository",
    "SqlAlchemyMaxAccountRepository",
]
