"""Smoke tests for #271 (Fix #271: bypass action handlers off __new__)."""

from __future__ import annotations

from apply_pilot.features.max.digest.api import _bypass_action_handlers


def test_bypass_action_handlers_returns_five_instances() -> None:
    """#271: bypass returns 5 handler instances of the expected classes."""
    from apply_pilot.features.messaging.actions.accept import AcceptActionHandler
    from apply_pilot.features.messaging.actions.defer import DeferActionHandler
    from apply_pilot.features.messaging.actions.regenerate import RegenerateActionHandler
    from apply_pilot.features.messaging.actions.reject import RejectActionHandler
    from apply_pilot.features.messaging.actions.review import ReviewActionHandler

    accept, defer, reject, review, regenerate = _bypass_action_handlers()
    assert isinstance(accept, AcceptActionHandler)
    assert isinstance(defer, DeferActionHandler)
    assert isinstance(reject, RejectActionHandler)
    assert isinstance(review, ReviewActionHandler)
    assert isinstance(regenerate, RegenerateActionHandler)
