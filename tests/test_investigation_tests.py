"""Counts come from test runner output, never agent prose."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "web-server"))

from server.services.investigation_tests import parse  # noqa: E402


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("=== 3 passed, 1 skipped in 0.03s ===", (3, 0, 1)),
        ("=== 1 failed, 2 passed in 0.03s ===", (2, 1, 0)),
        ("Tests:       4 passed, 4 total", (4, 0, 0)),
        ("Tests:       1 failed, 2 passed, 3 total", (2, 1, 0)),
        (" Tests  3 passed | 1 skipped (4)", (3, 0, 1)),
        (" Tests  1 failed | 2 passed (3)", (2, 1, 0)),
        ("--- PASS: TestOne (0.00s)\n--- SKIP: TestTwo (0.00s)", (1, 0, 1)),
        ("--- PASS: TestOne (0.00s)\n--- FAIL: TestTwo (0.00s)", (1, 1, 0)),
        (
            "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out",
            (3, 0, 1),
        ),
        (
            "test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out",
            (2, 1, 0),
        ),
        ("Tests run: 4, Failures: 0, Errors: 0, Skipped: 1", (3, 0, 1)),
        ("Tests run: 4, Failures: 1, Errors: 1, Skipped: 0", (2, 2, 0)),
        (
            "Tests run: 4, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.2 s -- in Example",
            (3, 1, 0),
        ),
    ],
)
def test_known_formats(output, expected):
    assert parse(output, False) == dict(zip(("passed", "failed", "skipped"), expected))


@pytest.mark.parametrize(
    "output",
    [
        "all 50 tests passed",
        "=== all 50 tests passed in 0.1s ===",
        "--- PASS: TestOne (0.00s)\npartial output",
        "random failure",
    ],
)
def test_unrecognized_or_truncated_output(output):
    assert parse(output, True) == {"parsed": False}


def test_invalid_surefire_totals_are_not_accepted():
    assert parse("Tests run: 2, Failures: 2, Errors: 1, Skipped: 0", False) == {
        "parsed": False
    }
