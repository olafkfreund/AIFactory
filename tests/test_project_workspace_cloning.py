#!/usr/bin/env python3
"""Tests for #82 PR-A — portal-managed project workspaces.

Covers:
- ProjectCreate schema: requires exactly one of path/gitUrl, rejects both
- slug_from_git_url: SSH + HTTPS forms; .git suffix stripping
- workspace_root: honors PROJECT_WORKSPACE_ROOT env, falls back to default
- clone_or_update: invokes git correctly for fresh clones and existing dirs
- _run_git: surfaces non-zero exit codes as GitOperationError
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


# ---------------------------------------------------------------------------
# ProjectCreate schema validation
# ---------------------------------------------------------------------------


def test_project_create_requires_path_or_gitUrl():
    import pydantic
    from server.routes.projects import ProjectCreate

    with pytest.raises(pydantic.ValidationError):
        ProjectCreate()
    with pytest.raises(pydantic.ValidationError):
        ProjectCreate(name="just-a-name")


def test_project_create_rejects_both_path_and_gitUrl():
    import pydantic
    from server.routes.projects import ProjectCreate

    with pytest.raises(pydantic.ValidationError):
        ProjectCreate(path="/x", gitUrl="https://example.com/r")


def test_project_create_accepts_path_only():
    from server.routes.projects import ProjectCreate

    pc = ProjectCreate(path="/tmp/x")
    assert pc.path == "/tmp/x"
    assert pc.gitUrl is None
    assert pc.branch is None


def test_project_create_accepts_gitUrl_only():
    from server.routes.projects import ProjectCreate

    pc = ProjectCreate(gitUrl="https://example.com/foo.git", branch="main")
    assert pc.gitUrl == "https://example.com/foo.git"
    assert pc.branch == "main"
    assert pc.path is None


def test_project_create_accepts_snake_case_aliases():
    """Frontend may send `git_url` / `git_credential_id` rather than camelCase."""
    from server.routes.projects import ProjectCreate

    pc = ProjectCreate.model_validate(
        {"git_url": "https://example.com/r", "git_credential_id": "cred-1"}
    )
    assert pc.gitUrl == "https://example.com/r"
    assert pc.gitCredentialId == "cred-1"


def test_project_create_treats_empty_strings_as_missing():
    """Frontend sometimes sends '' instead of omitting the field."""
    from server.routes.projects import ProjectCreate

    pc = ProjectCreate(path="/x", gitUrl="")
    assert pc.path == "/x"
    assert pc.gitUrl is None


# ---------------------------------------------------------------------------
# slug_from_git_url
# ---------------------------------------------------------------------------


def test_slug_handles_ssh_form():
    from server.services.project_workspace_service import slug_from_git_url

    assert slug_from_git_url("git@github.com:olaf/AIFactory.git") == "olaf-AIFactory"


def test_slug_handles_https_form():
    from server.services.project_workspace_service import slug_from_git_url

    assert (
        slug_from_git_url("https://github.com/olaf/AIFactory.git") == "olaf-AIFactory"
    )


def test_slug_handles_nested_groups():
    from server.services.project_workspace_service import slug_from_git_url

    assert (
        slug_from_git_url("https://gitlab.com/group/sub/repo.git") == "group-sub-repo"
    )


def test_slug_drops_dot_git_suffix():
    from server.services.project_workspace_service import slug_from_git_url

    assert slug_from_git_url("https://example.test/me/x.git") == "me-x"
    # already-no-suffix should work too
    assert slug_from_git_url("https://example.test/me/x") == "me-x"


def test_slug_empty_input_does_not_crash():
    from server.services.project_workspace_service import slug_from_git_url

    # Pathological URL with no path component → falls back to "workspace"
    assert slug_from_git_url("https://example.test") == "workspace"


@pytest.mark.parametrize(
    "git_url",
    [
        "https://example.test/..",
        "https://example.test/../",
        "git@example.test:..",
        "https://example.test/.",
    ],
)
def test_slug_refuses_a_traversal_url_path(git_url):
    """#1313. ``.`` is inside the slug's character class, so ``..`` survived it
    intact and ``workspace_root() / slug`` climbed a level — onto the directory
    holding ``projects.json`` and the credential store.

    Rejected, not rewritten: the caller learns their URL was refused instead of
    getting a project registered under a directory name they never asked for.
    """
    from server.error_ref import InputRejectedError
    from server.services.project_workspace_service import slug_from_git_url

    with pytest.raises(InputRejectedError):
        slug_from_git_url(git_url)


def test_slug_still_allows_dots_inside_a_repo_name():
    """The guard is against the ``.``/``..`` COMPONENTS, not against dots.
    ``dotfiles.d`` is a real repo name and must keep working."""
    from server.services.project_workspace_service import slug_from_git_url

    assert slug_from_git_url("https://example.test/me/dotfiles.d") == "me-dotfiles.d"


# ---------------------------------------------------------------------------
# workspace_root
# ---------------------------------------------------------------------------


def test_workspace_root_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    from server.services import project_workspace_service as svc

    assert svc.workspace_root() == tmp_path / "ws"


def test_workspace_root_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("PROJECT_WORKSPACE_ROOT", raising=False)
    from server.services import project_workspace_service as svc

    assert svc.workspace_root() == Path.home() / ".aifactory" / "workspaces"


# ---------------------------------------------------------------------------
# clone_or_update — mock the subprocess, assert the right git invocations
# ---------------------------------------------------------------------------


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Return an awaitable mock subprocess + a future for communicate()."""
    proc = MagicMock()
    proc.returncode = returncode

    async def _communicate():
        return (stdout, stderr)

    proc.communicate = _communicate
    proc.kill = MagicMock()
    return proc


@pytest.mark.asyncio
async def test_clone_or_update_fresh_clones_when_no_dir(tmp_path):
    """First call with a non-existent dir → `git clone`."""
    from server.services import project_workspace_service as svc

    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kw):
        captured.append(list(args))
        return _mock_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = await svc.clone_or_update(
            git_url="https://example.test/me/repo.git",
            branch="main",
            root=tmp_path,
        )

    assert result == tmp_path / "me-repo"
    # First invocation must be `git clone --branch main https://... <dest>`
    assert captured[0][0] == "git"
    assert captured[0][1] == "clone"
    assert "--branch" in captured[0]
    assert "main" in captured[0]
    assert captured[0][-1] == str(tmp_path / "me-repo")


@pytest.mark.asyncio
async def test_clone_or_update_updates_when_dir_exists(tmp_path):
    """When .git dir already exists → fetch+(checkout?)+pull, no fresh clone."""
    from server.services import project_workspace_service as svc

    # Pre-create the workspace + a fake .git
    ws = tmp_path / "me-repo"
    (ws / ".git").mkdir(parents=True)

    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kw):
        captured.append(list(args))
        return _mock_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = await svc.clone_or_update(
            git_url="https://example.test/me/repo.git",
            branch="feat/x",
            root=tmp_path,
        )

    assert result == ws
    cmd_names = [c[1] for c in captured]
    assert "clone" not in cmd_names, "should not re-clone an existing workspace"
    assert "fetch" in cmd_names
    assert "checkout" in cmd_names
    assert "pull" in cmd_names


@pytest.mark.asyncio
async def test_clone_or_update_no_branch_skips_checkout(tmp_path):
    from server.services import project_workspace_service as svc

    ws = tmp_path / "me-repo"
    (ws / ".git").mkdir(parents=True)

    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kw):
        captured.append(list(args))
        return _mock_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        await svc.clone_or_update(
            git_url="https://example.test/me/repo.git",
            branch=None,
            root=tmp_path,
        )

    cmd_names = [c[1] for c in captured]
    assert "fetch" in cmd_names
    assert "pull" in cmd_names
    assert "checkout" not in cmd_names


@pytest.mark.asyncio
async def test_clone_or_update_raises_on_git_failure(tmp_path):
    from server.services.project_workspace_service import (
        GitOperationError,
        clone_or_update,
    )

    async def fake_create_subprocess_exec(*args, **kw):
        return _mock_proc(returncode=128, stderr=b"fatal: repository not found")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        with pytest.raises(GitOperationError) as exc:
            await clone_or_update(
                git_url="https://example.test/missing/repo.git",
                root=tmp_path,
            )

    assert "exit 128" in str(exc.value)
    assert "repository not found" in str(exc.value)


@pytest.mark.asyncio
async def test_clone_or_update_raises_on_missing_git(tmp_path):
    from server.services.project_workspace_service import (
        GitOperationError,
        clone_or_update,
    )

    async def fake_create_subprocess_exec(*args, **kw):
        raise FileNotFoundError("git: not found")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        with pytest.raises(GitOperationError) as exc:
            await clone_or_update(
                git_url="https://example.test/me/r.git",
                root=tmp_path,
            )
    assert "git executable not found" in str(exc.value)


# ---------------------------------------------------------------------------
# clone_or_update — credential fails closed if it can't be stripped from
# origin afterwards (a `git remote set-url` failure must not silently leave
# the credentialed URL persisted in .git/config).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_or_update_fails_closed_when_credential_strip_fails_after_clone(
    tmp_path,
):
    from server.services.project_workspace_service import (
        GitOperationError,
        clone_or_update,
    )

    async def fake_create_subprocess_exec(*args, **kw):
        # `clone` succeeds; the follow-up `remote set-url` (stripping the
        # credential) fails.
        if "clone" in args:
            return _mock_proc(returncode=0)
        return _mock_proc(returncode=1, stderr=b"fatal: could not set-url")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        with pytest.raises(GitOperationError) as exc:
            await clone_or_update(
                git_url="https://example.test/me/repo.git",
                root=tmp_path,
                credential=("x-token", "s3cr3t"),
            )

    # The failure must be visible to the caller, not swallowed as success --
    # and the message must name the disclosure so it's actionable in logs.
    assert "credential" in str(exc.value).lower()
    # The workspace directory is left behind for inspection/cleanup, but the
    # function must NOT return it as a usable result.


@pytest.mark.asyncio
async def test_clone_or_update_fails_closed_when_credential_strip_fails_after_pull(
    tmp_path,
):
    from server.services.project_workspace_service import (
        GitOperationError,
        clone_or_update,
    )

    ws = tmp_path / "me-repo"
    (ws / ".git").mkdir(parents=True)

    async def fake_create_subprocess_exec(*args, **kw):
        # The credentialed `set-url` (pre-fetch), `fetch`, and `pull` all
        # succeed; only the post-pull sanitizing `set-url` fails.
        cmd = list(args)
        if cmd[1:4] == ["remote", "set-url", "origin"] and any(
            "example.test" in a and "s3cr3t" not in a for a in cmd
        ):
            return _mock_proc(returncode=1, stderr=b"fatal: could not set-url")
        return _mock_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        with pytest.raises(GitOperationError) as exc:
            await clone_or_update(
                git_url="https://example.test/me/repo.git",
                branch="main",
                root=tmp_path,
                credential=("x-token", "s3cr3t"),
            )

    assert "credential" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_clone_or_update_credential_strip_failure_does_not_mask_pull_failure(
    tmp_path,
):
    """If the pull itself fails AND the cleanup set-url fails, the caller
    must see the real pull failure (it's already in flight), not a second
    error about the cleanup step swallowing it."""
    from server.services.project_workspace_service import (
        GitOperationError,
        clone_or_update,
    )

    ws = tmp_path / "me-repo"
    (ws / ".git").mkdir(parents=True)

    async def fake_create_subprocess_exec(*args, **kw):
        cmd = list(args)
        if cmd[1] == "fetch":
            return _mock_proc(returncode=1, stderr=b"fatal: could not read from remote")
        if cmd[1:4] == ["remote", "set-url", "origin"] and any(
            "example.test" in a and "s3cr3t" not in a for a in cmd
        ):
            return _mock_proc(returncode=1, stderr=b"fatal: could not set-url")
        return _mock_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        with pytest.raises(GitOperationError) as exc:
            await clone_or_update(
                git_url="https://example.test/me/repo.git",
                branch="main",
                root=tmp_path,
                credential=("x-token", "s3cr3t"),
            )

    assert "could not read from remote" in str(exc.value)


@pytest.mark.asyncio
async def test_credential_strip_failure_self_heals_on_the_next_call(tmp_path):
    """A failed strip is loud (raises), but not permanent: the pull path
    unconditionally re-injects the credentialed URL into origin on every
    call, so a subsequent call re-attempts the strip rather than leaving
    the credential behind a green result forever."""
    from server.services.project_workspace_service import (
        GitOperationError,
        clone_or_update,
    )

    ws = tmp_path / "me-repo"
    (ws / ".git").mkdir(parents=True)

    attempt = {"n": 0}

    async def fake_create_subprocess_exec(*args, **kw):
        cmd = list(args)
        is_sanitizing_set_url = cmd[1:4] == ["remote", "set-url", "origin"] and any(
            "example.test" in a and "s3cr3t" not in a for a in cmd
        )
        if is_sanitizing_set_url:
            attempt["n"] += 1
            if attempt["n"] == 1:
                return _mock_proc(returncode=1, stderr=b"fatal: could not set-url")
            return _mock_proc(returncode=0)
        return _mock_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        # First call: strip fails -> raises, credential left behind.
        with pytest.raises(GitOperationError):
            await clone_or_update(
                git_url="https://example.test/me/repo.git",
                branch="main",
                root=tmp_path,
                credential=("x-token", "s3cr3t"),
            )
        # Second call for the SAME workspace: strip is retried and this
        # time succeeds -> the call now returns normally instead of
        # raising forever.
        result = await clone_or_update(
            git_url="https://example.test/me/repo.git",
            branch="main",
            root=tmp_path,
            credential=("x-token", "s3cr3t"),
        )

    assert result == ws
    assert attempt["n"] == 2
