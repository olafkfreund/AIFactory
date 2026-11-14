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
    "swap",
    [["replace", "{base}", "HEAD"], ["update-ref", "refs/replace/{base}", "HEAD"]],
)
def test_replace_refs_cannot_swap_base(repo: Path, swap: list[str]) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
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


def _agent_adds_colliding_names(repo: Path) -> None:
    """Targets and a script whose names pass the other checks (digits, `echo`)."""
    (repo / "Makefile").write_text(
        "test:\n\techo ok\n123 2.5 \u00b2:\n\techo pwn\n", encoding="utf-8"
    )
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "echo ok", "echo": "echo pwn"}})
    )
    (repo / "evil.mk").write_text("test:\n\techo pwn\n")
    reset_profile_cache()


@pytest.mark.parametrize(
    "cmd",
    [
        # GNU make accepts abbreviated long options.
        "make --fil=evil.mk test",
        "make --mak=evil.mk test",
        "make --dir=sub test",
        "make '--ev=$(shell ./evil.sh)' test",
        "make --include-d=sub test",
        # Variables and environment that change what make reads or runs.
        "make SHELL=./evil.sh test",
        "make MAKEFILES=evil.mk test",
        "MAKEFLAGS=-fevil.mk make test",
        "MAKEFILES=evil.mk make test",
        "env MAKEFLAGS=-fevil.mk make test",
        "export MAKEFLAGS=-fevil.mk",
        # A numeric token is a target unless it follows -j/-l.
        "make 123",
        "make -k 123",
        "make -o -j 123",
        "make -- -evil",
        "make 'SHELL := ./evil.sh' test",
        "make 'SHELL =./evil.sh' test",
        "make .DEFAULT_GOAL=123",
        "make -j 2.5",
        "make -j \u00b2",
        "make 'X!=./evil.sh' test",
        "make 'X != ./evil.sh' test",
        "make 'X:=$(shell ./evil.sh)' test",
        "npm ur echo",
        # npm runs unambiguous command prefixes.
        "npm run-s echo",
        "npm resta",
        # npm options as environment, and the script shell.
        "npm_config_workspace=sub npm run test",
        "NPM_CONFIG_PREFIX=sub npm run test",
        "npm run test --script-shell=./evil.sh",
        # Shorthand without `run`, with a script named like an allowed command.
        "yarn echo",
        "pnpm echo",
        "bun echo",
        "bun run echo",
    ],
)
def test_review_bypasses_refused(repo: Path, cmd: str) -> None:
    _agent_adds_colliding_names(repo)
    assert not validate_command(cmd, repo)[0], cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "make -j 4 test",
        "make -kj 4 test",
        "make --jobs 4 test",
        "make -l 2.5 test",
        "make -Onone test",
        "npm ts",
        "make -- test",
        "npm install",
        "npm ins",
        "npm ci",
        "npm tes",
        "make V=1 test",
        "yarn install",
        "pnpm install",
        "yarn test",
        "bun test",
        "bun install",
    ],
)
def test_review_fixes_keep_normal_use(repo: Path, cmd: str) -> None:
    _agent_adds_colliding_names(repo)
    assert validate_command(cmd, repo)[0], cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "npm it",
        "npm install-test",
        "pnpm it",
        "npm tes",
        "npm install-t",
        "npm ts",
        "npm clean-install-t",
    ],
)
def test_install_test_aliases_need_base_test_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", str(tmp_path / "bl"))
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init")
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"lint": "true"}}))
    _git(repo_dir, "add", "package.json")
    _git(repo_dir, "commit", "-m", "base")
    record_base_sha(repo_dir)
    (repo_dir / "package.json").write_text(
        json.dumps({"scripts": {"lint": "true", "test": "echo pwn"}})
    )
    reset_profile_cache()
    assert not validate_command(cmd, repo_dir)[0], cmd


def test_nested_repo_in_recorded_worktree_grants_nothing(repo: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    _git(sub, "init")
    (sub / "Makefile").write_text("evil:\n\techo pwn\n")
    (sub / "evil.sh").write_text("#!/bin/sh\necho pwn\n")
    _git(sub, "add", "Makefile", "evil.sh")
    _git(sub, "commit", "-m", "nested")
    reset_profile_cache()
    profile = get_security_profile(sub)
    assert "make" not in profile.script_commands
    assert profile.custom_scripts.shell_scripts == []
    assert not validate_command("make evil", sub)[0]


def test_moved_gitdir_keeps_base(repo: Path) -> None:
    _git(repo, "init", "--separate-git-dir=" + str(repo / "newgit"))
    reset_profile_cache()
    assert validate_command("make test", repo)[0]
    assert not validate_command("make evil", repo)[0]
    assert not validate_command("npm run evil", repo)[0]


@pytest.mark.parametrize(
    "text",
    [
        "\ufeff" + json.dumps({"scripts": {"echo": "echo pwn"}}),
        '{"scripts": {"echo": "echo pwn",}}',
        '{"scripts": {"echo": "echo pwn"}, "scripts": {}}',
    ],
    ids=["bom", "trailing-comma", "duplicate-key"],
)
def test_loosely_parsed_package_json_fails_closed(repo: Path, text: str) -> None:
    (repo / "package.json").write_text(text, encoding="utf-8")
    reset_profile_cache()
    for cmd in ("yarn echo hi", "bun echo hi", "bun run echo hi"):
        assert not validate_command(cmd, repo)[0], cmd
