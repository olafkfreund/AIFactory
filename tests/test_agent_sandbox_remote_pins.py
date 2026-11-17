"""Vendored agent_sandbox: remote program keys are refused, tampering is detected (#3737)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from agent_sandbox import check_git_metadata, merge_driver_pins, record_baseline


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)  # noqa: S603, S607 - test argv


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FACTORY_GIT_BASELINE_DIR", str(tmp_path / "baselines"))
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(r)], check=True)  # noqa: S603, S607
    return r


@pytest.mark.parametrize("key", ["remote.evil.uploadpack", "remote.origin.receivepack"])
def test_unpinned_remote_program_is_rejected(repo: Path, key: str) -> None:
    _git(repo, "config", key, "/tmp/x")
    with pytest.raises(RuntimeError, match="cannot be pinned"):
        merge_driver_pins(repo, env={})


def test_tampered_git_metadata_is_detected(repo: Path) -> None:
    record_baseline(repo)
    _git(repo, "config", "merge.x.driver", "touch /tmp/pwned")
    with pytest.raises(RuntimeError, match="git metadata changed"):
        check_git_metadata(repo)
