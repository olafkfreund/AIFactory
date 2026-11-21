"""Pure scope-ceiling rules for project authorization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes.project_authz import scope_ceiling  # noqa: E402


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (None, "owner"),
        ([], None),
        (["unknown"], None),
        (["mcp:read"], "viewer"),
        (["project:read", "task:read", "*:read"], "viewer"),
        (["api"], "member"),
        (["project:write", "mcp:read"], "member"),
        (["task:merge", "project:write"], "admin"),
        (["*:admin"], "admin"),
    ],
)
def test_scope_ceiling(scopes, expected):
    assert scope_ceiling(scopes) == expected
