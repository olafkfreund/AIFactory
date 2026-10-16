"""LFS objects must land before either backend branch push (#1721)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core import child_env, workspace_fetch  # noqa: E402
from pfactory import tfactory_client  # noqa: E402


@pytest.mark.parametrize("upload_fails", [True, False], ids=["failure", "success"])
def test_workspace_upload_precedes_push(monkeypatch, tmp_path, upload_fails):
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/workspace")
    monkeypatch.setattr(child_env.shutil, "which", lambda _: "/usr/bin/git-lfs")
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x"
    wt.mkdir(parents=True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["git", "rev-parse"]:
            out = "aifactory/042-x"
        elif argv[:3] == ["git", "remote", "get-url"]:
            out = "https://github.com/o/r"
        else:
            out = ""
        rc = 1 if argv[:2] == ["git", "config"] else 0
        if "lfs" in argv and "push" in argv and upload_fails:
            rc = 2
        return SimpleNamespace(
            returncode=rc, stdout=out, stderr="boom" if rc == 2 else ""
        )

    monkeypatch.setattr(workspace_fetch.subprocess, "run", fake_run)
    assert workspace_fetch.maybe_push_workspace_branch(tmp_path, "042-x") is True
    argv = [call[0] for call in calls]
    upload = next(i for i, args in enumerate(argv) if "lfs" in args and "push" in args)
    pushes = [i for i, args in enumerate(argv) if args[:2] == ["git", "push"]]
    assert argv[upload][-1] == "HEAD"
    assert calls[upload][1]["timeout"] == 600
    assert all(call[1]["env"] is calls[upload][1]["env"] for call in calls[2:])
    assert pushes == [upload + 1]


@pytest.mark.parametrize("upload_fails", [True, False], ids=["failure", "success"])
def test_handoff_upload_precedes_push(monkeypatch, tmp_path, upload_fails):
    monkeypatch.setattr(child_env.shutil, "which", lambda _: "/usr/bin/git-lfs")
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = "a" * 40
    url = "https://github.com/o/r"
    monkeypatch.setattr(tfactory_client, "_locate_build_commit", lambda *_: (repo, sha))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:3] == ["git", "remote", "get-url"]:
            out = url
        elif argv[:2] == ["git", "ls-remote"]:
            out = f"{sha}\trefs/heads/aifactory/042-x"
        else:
            out = ""
        rc = 1 if argv[:2] == ["git", "config"] else 0
        if "lfs" in argv and "push" in argv and upload_fails:
            rc = 2
        return SimpleNamespace(
            returncode=rc, stdout=out, stderr="boom" if rc == 2 else ""
        )

    monkeypatch.setattr(tfactory_client.subprocess, "run", fake_run)
    assert tfactory_client._git_info_and_push(tmp_path, "042-x") == (
        url,
        "aifactory/042-x",
    )
    argv = [call[0] for call in calls]
    upload = next(i for i, args in enumerate(argv) if "lfs" in args and "push" in args)
    pushes = [i for i, args in enumerate(argv) if args[:2] == ["git", "push"]]
    assert argv[upload][-1] == sha
    assert calls[upload][1]["timeout"] == 600
    assert all(call[1]["env"] is calls[upload][1]["env"] for call in calls[1:])
    assert pushes == [upload + 1]


@pytest.mark.parametrize("has_filter", [False, True], ids=["raw-blobs", "filter-on"])
def test_workspace_lfs_repo_on_gitlab(monkeypatch, tmp_path, has_filter):
    # Non-GitHub remotes push as on dev: get-url sees gitlab.com and returns None
    # before the gate or filter check (#1721).
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/workspace")
    monkeypatch.setattr(child_env.shutil, "which", lambda _: "/usr/bin/git-lfs")
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x").mkdir(parents=True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        out = {"rev-parse": "aifactory/042-x", "remote": "https://gitlab.com/o/r"}
        rc = 0 if has_filter or "filter.lfs.clean" not in argv else 1
        return SimpleNamespace(returncode=rc, stdout=out.get(argv[1], ""), stderr="")

    monkeypatch.setattr(workspace_fetch.subprocess, "run", fake_run)
    assert workspace_fetch.maybe_push_workspace_branch(tmp_path, "042-x") is True
    assert any(argv[:2] == ["git", "push"] for argv in calls)
    assert not any(argv[:2] == ["git", "grep"] for argv in calls)
    assert not any("lfs" in argv and "push" in argv for argv in calls)


def test_workspace_safety_net_refusal_does_not_push(monkeypatch, tmp_path, caplog):
    # GitHub https origin rejected by lfs_push_argv + local filter.lfs.clean set:
    # the safety-net refusal is never softened by best_effort (#1721).
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/workspace")
    monkeypatch.setattr(child_env.shutil, "which", lambda _: "/usr/bin/git-lfs")
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x").mkdir(parents=True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        out = {"rev-parse": "aifactory/042-x", "remote": "https://github.com/o/r/x"}
        return SimpleNamespace(returncode=0, stdout=out.get(argv[1], ""), stderr="")

    monkeypatch.setattr(workspace_fetch.subprocess, "run", fake_run)
    with caplog.at_level("WARNING", logger="core.workspace_fetch"):
        assert workspace_fetch.maybe_push_workspace_branch(tmp_path, "042-x") is False
    assert not any(argv[:2] == ["git", "push"] for argv in calls)
    assert "no supported LFS endpoint" in caplog.text


def test_handoff_lfs_timeout_still_pushes(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(child_env.shutil, "which", lambda _: "/usr/bin/git-lfs")
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = "a" * 40
    url = "https://github.com/o/r"
    monkeypatch.setattr(tfactory_client, "_locate_build_commit", lambda *_: (repo, sha))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "lfs" in argv and "push" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        rc = 1 if argv[:2] == ["git", "config"] else 0
        if argv[:3] == ["git", "remote", "get-url"]:
            out = url
        elif argv[:2] == ["git", "ls-remote"]:
            out = f"{sha}\trefs/heads/aifactory/042-x"
        else:
            out = ""
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(tfactory_client.subprocess, "run", fake_run)
    with caplog.at_level("WARNING", logger="core.child_env"):
        assert tfactory_client._git_info_and_push(tmp_path, "042-x") == (
            url,
            "aifactory/042-x",
        )
    assert any(argv[:2] == ["git", "push"] for argv in calls)
    assert "timed out" in caplog.text
