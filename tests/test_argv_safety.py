#!/usr/bin/env python3
"""
Tests for the subprocess argv boundary (#1267).
===============================================

Every subprocess on these paths uses the list form, so the risk is argument
injection rather than shell injection: a value that begins with ``-`` is read
by the program as an option. ``git log --output=<file>`` turns that from a bad
ref into an arbitrary file write, so these are the tests that go red if a
validator is weakened.

Three layers:

1. ``services/argv_safety.py`` -- the validators themselves.
2. ``routes/changelog.py`` -- the one site that was genuinely injectable: a
   changelog request body reached a ``git log`` argv with no validation at all.
3. ``routes/worktree_tools.py`` -- the launcher no longer comes from the
   request body, and no argv entry is a shell fragment built from the path.
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Add web-server to path so server modules are importable
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services.argv_safety import (  # noqa: E402
    assert_not_option,
    assert_safe_git_ref,
    bounded_count,
)

# ---------------------------------------------------------------------------
# 1. The validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "HEAD",
        "feature/add-thing",
        "v1.2.3",
        "HEAD~1",
        "origin/main",
    ],
)
def test_assert_safe_git_ref_accepts_real_refs(ref):
    assert assert_safe_git_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "--output=/tmp/pwned",  # the file-write primitive
        "-n",
        "--all",
        "a..b",  # would rewrite the range it is joined into
        "main;rm -rf /",
        "main branch",
        "",
        "main\nsecond",
    ],
)
def test_assert_safe_git_ref_rejects_option_and_separator_shapes(ref):
    with pytest.raises(ValueError):
        assert_safe_git_ref(ref)


def test_assert_not_option_accepts_operands():
    assert assert_not_option("src/app.py") == "src/app.py"
    assert assert_not_option("") == ""
    assert assert_not_option("*.py") == "*.py"


@pytest.mark.parametrize("value", ["-x", "--pre=/bin/sh", "a\x00b"])
def test_assert_not_option_rejects(value):
    with pytest.raises(ValueError):
        assert_not_option(value)


def test_bounded_count():
    assert bounded_count("20", 100) == 20
    for bad in ["0", "101", "abc", None, "-5"]:
        with pytest.raises(ValueError):
            bounded_count(bad, 100)


# ---------------------------------------------------------------------------
# 2. The changelog endpoint (the genuinely injectable site)
# ---------------------------------------------------------------------------


def _commits_preview(monkeypatch, tmp_path, options, mode):
    """Drive the endpoint with subprocess.run captured, return (result, argv)."""
    from server.routes import changelog as changelog_routes

    seen = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(
        "server.routes.projects.load_projects",
        lambda: {"p1": {"path": str(tmp_path)}},
    )

    request = changelog_routes.CommitsPreviewRequest(options=options, mode=mode)
    result = asyncio.run(
        changelog_routes.get_commits_preview(projectId="p1", request=request)
    )
    return result, seen.get("cmd")


def test_commits_preview_rejects_option_shaped_branch(monkeypatch, tmp_path):
    """A ref of `--output=<file>` must never reach the git argv."""
    result, cmd = _commits_preview(
        monkeypatch,
        tmp_path,
        {"baseBranch": "--output=/tmp/pwned", "compareBranch": "HEAD"},
        "branch-diff",
    )
    assert result["success"] is False
    assert "baseBranch" in result["error"]
    assert cmd is None  # git was never invoked


def test_commits_preview_rejects_option_shaped_tag(monkeypatch, tmp_path):
    result, cmd = _commits_preview(
        monkeypatch,
        tmp_path,
        {"type": "tag-range", "fromTag": "--output=/tmp/pwned", "toTag": "HEAD"},
        "history",
    )
    assert result["success"] is False
    assert cmd is None


def test_commits_preview_allows_ordinary_branches(monkeypatch, tmp_path):
    result, cmd = _commits_preview(
        monkeypatch,
        tmp_path,
        {"baseBranch": "main", "compareBranch": "feature/x"},
        "branch-diff",
    )
    assert result["success"] is True
    assert cmd[-1] == "main..feature/x"


def test_commits_preview_bounds_count(monkeypatch, tmp_path):
    result, cmd = _commits_preview(
        monkeypatch, tmp_path, {"type": "last-n", "count": "999999"}, "history"
    )
    assert result["success"] is False
    assert cmd is None


# ---------------------------------------------------------------------------
# 3. The launcher endpoints
# ---------------------------------------------------------------------------


def test_launcher_is_never_taken_from_the_request_body():
    """`customPath` was arbitrary program execution; it is refused, not ignored.

    Dropping the field would let pydantic discard it silently, and the server
    would launch a different program than the caller asked for while reporting
    success. The field is kept so the request can fail loudly (#1267).
    """
    from fastapi import HTTPException
    from server.routes import worktree_tools

    for model, handler, kwargs in (
        (
            worktree_tools.OpenInIDERequest,
            worktree_tools.open_worktree_in_ide,
            {"ide": "vscode"},
        ),
        (
            worktree_tools.OpenInTerminalRequest,
            worktree_tools.open_worktree_in_terminal,
            {"terminal": "kitty"},
        ),
    ):
        assert "customPath" in model.model_fields
        request = model(worktreePath="/tmp", customPath="/bin/sh", **kwargs)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(handler(request))
        assert exc.value.status_code == 400
        assert "customPath" in exc.value.detail
        assert "#1267" in exc.value.detail

        # Absent or empty is the normal case and must not 400.
        worktree_tools.reject_custom_path(None)
        worktree_tools.reject_custom_path("")


def test_no_terminal_command_embeds_the_path_in_a_shell_fragment(tmp_path):
    """No argv entry may be a shell string built from the path.

    `x-terminal-emulator -e "cd <path> && $SHELL"` handed the path to a shell,
    so a directory named `x; curl evil` was command execution. Popen(cwd=...)
    replaced it.
    """
    marker = ";curl-evil"
    for terminal in ("system", "xterm", "kitty", "gnome-terminal", "wt", "cmd"):
        cmd = worktree_terminal_cmd(terminal, f"/tmp/{marker}")
        for arg in cmd:
            assert "&&" not in arg
            assert not arg.startswith("cd ")


def worktree_terminal_cmd(terminal: str, path: str) -> list[str]:
    from server.routes.worktree_tools import get_terminal_command

    return get_terminal_command(terminal, path)


def test_resolve_launch_dir_rejects_non_directories(tmp_path):
    from server.routes.worktree_tools import resolve_launch_dir

    ok, err = resolve_launch_dir(str(tmp_path))
    assert err is None and ok == str(tmp_path.resolve())

    missing, err = resolve_launch_dir(str(tmp_path / "nope"))
    assert err is not None and missing == ""


def test_worktree_name_pattern_rejects_leading_dash():
    from server.services.terminal_worktree_service import TerminalWorktreeService

    service = TerminalWorktreeService.__new__(TerminalWorktreeService)
    with pytest.raises(ValueError):
        service._validate_name("-force")
    assert service._validate_name("my_worktree-2") == "my_worktree-2"
