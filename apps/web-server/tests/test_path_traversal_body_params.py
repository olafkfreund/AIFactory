"""Body-sourced path components must be validated; path params are not enough.

The `{projectId}` / `{task_id}` route parameters cannot carry a separator --
Starlette compiles them to `[^/]+`, and percent-encoded forms are decoded
before routing, so `..%2f..%2f` 404s rather than reaching the handler. That is
the reason most of this repo's residual path-injection alerts are not
exploitable, and the first test pins it: it is load-bearing for that argument,
so it must fail loudly if a route is ever declared `{path:path}`.

Request BODY fields have no such constraint, which is what these tests cover.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.specpath import safe_spec_component


def test_path_parameters_cannot_carry_a_separator():
    app = FastAPI()
    reached: list[str] = []

    @app.get("/p/{projectId}/x")
    def _h(projectId: str):
        reached.append(projectId)
        return {}

    client = TestClient(app)
    for probe in (
        "../../etc/passwd",
        "..%2f..%2fetc",
        "a%2F..%2Fb",
        "%2e%2e%2f%2e%2e%2fetc",
    ):
        assert client.get(f"/p/{probe}/x").status_code == 404, probe
    assert reached == []


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc",
        "..",
        ".",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "spec\x00",
    ],
)
def test_load_task_specs_rejects_traversal_in_taskids(hostile):
    """`taskIds` is a body list joined onto specs_dir AND into a glob pattern."""
    with pytest.raises(ValueError):
        safe_spec_component(hostile, "taskId")


def test_load_task_specs_still_accepts_a_real_task_id():
    assert safe_spec_component("001-feature-name", "taskId") == "001-feature-name"


def test_worktree_name_check_is_anchored_at_both_ends():
    """`re.match(r"^...$")` also matches before a TRAILING NEWLINE.

    That is why the check in routes/git.py is a `fullmatch`. Asserting the
    property here rather than importing the route keeps the test honest about
    what actually changed.
    """
    pattern = r"[a-zA-Z0-9_-]+"
    assert re.match(rf"^{pattern}$", "ok\n") is not None  # the old form accepted it
    assert re.fullmatch(pattern, "ok\n") is None  # the new form does not
    assert re.fullmatch(pattern, "ok") is not None


def test_terminal_worktree_name_rejects_a_trailing_newline():
    """The pattern is anchored, but `$` matches before a final newline too.

    The value becomes a directory under `.aifactory/worktrees` AND a
    `terminal/<name>` git branch, so a name carrying a character the validator
    was written to reject is worth failing on even though it is not traversal.
    """
    from server.services.terminal_worktree_service import TerminalWorktreeService

    pat = TerminalWorktreeService.WORKTREE_NAME_PATTERN
    assert pat.fullmatch("ok-name_1") is not None
    assert pat.fullmatch("ok\n") is None
    assert pat.fullmatch("../etc") is None
    assert pat.fullmatch("-opt") is None  # cannot be read as a git option
