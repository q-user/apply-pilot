"""Tests for core module."""

from job_apply.core import greet


def test_greet() -> None:
    """Test greet function."""
    assert greet("World") == "Hello, World!"