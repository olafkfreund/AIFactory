"""Tests for the arithmetic helpers in R1."""

from R1 import add


def test_add():
    """add(2, 3) should return 5."""
    assert add(2, 3) == 5
