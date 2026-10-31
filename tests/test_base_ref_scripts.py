"""#1948: script runners only come from files tracked at the base ref."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from core.git_metadata import record_base_sha
from security.hooks import validate_command
from security.profile import get_security_profile, reset_profile_cache


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", str(tmp_path / "bl"))
    reset_profile_cache()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init")
    (repo_dir / "Makefile").write_text("test:\n\techo ok\n")
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"test": "echo ok"}}))
    (repo_dir / "build.sh").write_text("#!/bin/sh\necho ok\n")
    _git(repo_dir, "add", "Makefile", "package.json", "build.sh")
    _git(repo_dir, "commit", "-m", "base")
    record_base_sha(repo_dir)

    (repo_dir / "Makefile").write_text("test:\n\techo ok\nevil:\n\techo pwn\n")
    (repo_dir / "package.json").write_text(
        json.dumps({"scripts": {"test": "echo ok", "evil": "echo pwn"}})
    )
    _git(repo_dir, "add", "Makefile", "package.json")
    _git(repo_dir, "commit", "-m", "agent edits")
    (repo_dir / "evil.sh").write_text("#!/bin/sh\necho pwn\n")
    return repo_dir


def test_scripts_pinned_to_base_sha(repo: Path) -> None:
    for cmd in ("make test", "npm test", "npm run test", "npm install -w pkg"):
        assert validate_command(cmd, repo)[0], cmd

    for cmd in (
        "make evil",
        "npm run evil",
        "yarn run evil",
        "pnpm run evil",
        "make -f x.mk test",
        "npm --prefix sub run test",
        "npm -w pkg test",
        "./evil.sh",
    ):
        assert not validate_command(cmd, repo)[0], cmd

    assert get_security_profile(repo).custom_scripts.shell_scripts == ["build.sh"]


@pytest.mark.parametrize(
    "swap", [["replace", "{base}", "HEAD"], ["update-ref", "refs/replace/{base}", "HEAD"]]
)
def test_replace_refs_cannot_swap_base(repo: Path, swap: list[str]) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, *(a.format(base=base) for a in swap))
    reset_profile_cache()
    assert validate_command("make test", repo)[0]
    assert not validate_command("make evil", repo)[0]
    assert not validate_command("npm run evil", repo)[0]


def test_no_makefile_at_base_disables_make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", str(tmp_path / "bl"))
    reset_profile_cache()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init")
    (repo_dir / "README.md").write_text("# hi\n")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-m", "base")
    record_base_sha(repo_dir)

    (repo_dir / "Makefile").write_text("evil:\n\techo pwn\n")
    _git(repo_dir, "add", "Makefile")
    _git(repo_dir, "commit", "-m", "add makefile")

    profile = get_security_profile(repo_dir)
    assert "make" not in profile.script_commands
    assert not validate_command("make evil", repo_dir)[0]
