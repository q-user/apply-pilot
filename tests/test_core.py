"""Tests for core module."""

from apply_pilot.core import greet


def test_greet() -> None:
    """Test greet function."""
    assert greet("World") == "Hello, World!"
