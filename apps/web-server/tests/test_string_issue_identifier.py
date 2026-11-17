"""RFC-0022 phase 1 (Factory#3774): issue identifiers are strings on the provider seam."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from runners.github.providers.protocol import as_issue_int  # noqa: E402
from server.services.completion import correlation_key  # noqa: E402


@pytest.mark.parametrize("ref", [5, "5"])
def test_correlation_key_is_identical_for_int_and_decimal_string(ref):
    assert correlation_key("p:s", ref, "o/r") == "o/r#5"


def test_as_issue_int_rejects_non_numeric_key_at_the_edge():
    assert as_issue_int("5") == 5
    with pytest.raises(ValueError, match="PROJ-1"):
        as_issue_int("PROJ-1", "GitHub")


# Provider identifiers are strings: an `== <int>` comparison is silently False.
_INT_COMPARE = re.compile(
    r"\b(?:issue_number|issue\.number|iss\.number|issue_ref)\s*[!=]=\s*\d+"
)


def test_no_int_literal_comparison_against_issue_identifiers():
    hits = [
        f"{p.relative_to(ROOT)}: {m.group(0)}"
        for base in (
            ROOT / "apps/web-server/server",
            ROOT / "apps/backend/runners/github",
        )
        for p in base.rglob("*.py")
        if "test" not in p.name
        for m in _INT_COMPARE.finditer(p.read_text())
    ]
    assert not hits, hits
