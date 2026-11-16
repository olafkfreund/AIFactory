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
        "env '.SHELLFLAGS=-c ./evil.sh;' make test",
        "env .EXTRA_PREREQS=123 make test",
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
        # Shapes make or the shell rewrite before the target check.
        "make -lj 123",
        "make -l2j 123",
        "make '$(shell ./evil.sh)X=1' test",
        "A=SHELL; make $A=./evil.sh test",
        "A='f evil.mk'; make -$A test",
        "make {SHELL,V}=./evil.sh test",
        "make SHEL?=./evil.sh test",
        "make --unknown-option test",
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
        "make -j4 test",
        "make -s --keep-going test",
        "make --output-sync=target test",
        "make CFLAGS='-O2 -g' test",
        "make -n test",
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


# --- #2028: follow-ups from the #1948 bypass reviews ---


def _rev(repo: Path, spec: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", spec], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _loose(repo: Path, oid: str) -> Path:
    return repo / ".git" / "objects" / oid[:2] / oid[2:]


@pytest.mark.parametrize("kind", ["commit", "tree", "blob"])
def test_forged_base_object_is_ignored(repo: Path, kind: str) -> None:
    """A loose object overwritten with other content must not stand in (#2028)."""
    oids = {
        "commit": (_rev(repo, "HEAD~1"), _rev(repo, "HEAD")),
        "tree": (_rev(repo, "HEAD~1^{tree}"), _rev(repo, "HEAD^{tree}")),
        "blob": (_rev(repo, "HEAD~1:Makefile"), _rev(repo, "HEAD:Makefile")),
    }
    victim, forged = (_loose(repo, o) for o in oids[kind])
    victim.chmod(0o644)
    victim.write_bytes(forged.read_bytes())
    reset_profile_cache()
    assert not validate_command("make evil", repo)[0]
    assert not validate_command("npm run evil", repo)[0]


def test_tag_named_like_base_sha_is_ignored(repo: Path) -> None:
    base = _rev(repo, "HEAD~1")
    _loose(repo, base).chmod(0o644)
    _loose(repo, base).unlink()
    _git(repo, "tag", base, "HEAD")
    reset_profile_cache()
    assert not validate_command("make evil", repo)[0]


@pytest.mark.parametrize(
    "cmd",
    [
        "./echo hi",
        "cd sub && ./echo hi",
        "sub/echo hi",
        "env ./echo hi",
        "/tmp/echo hi",
        "/usr/bin/../../tmp/echo hi",
        "./build.sh",
        "./evil.sh",
        ".venv/bin/python -V",
    ],
)
def test_path_bearing_commands_refused(repo: Path, cmd: str) -> None:
    (repo / "echo").write_text("#!/bin/sh\necho pwn\n")
    assert not validate_command(cmd, repo)[0], cmd


@pytest.mark.parametrize("cmd", ["echo hi", "/usr/bin/env true", "/bin/echo hi"])
def test_bare_and_system_commands_still_allowed(repo: Path, cmd: str) -> None:
    assert validate_command(cmd, repo)[0], cmd


def _repo_with_makefile(tmp_path: Path, monkeypatch, makefile: str, **files) -> Path:
    monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", str(tmp_path / "bl"))
    reset_profile_cache()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init")
    (repo_dir / "Makefile").write_text(makefile)
    for name, text in files.items():
        (repo_dir / name).write_text(text)
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "base")
    record_base_sha(repo_dir)
    return repo_dir


def test_makefile_target_shapes(tmp_path: Path, monkeypatch) -> None:
    mk = (
        "include common.mk\n"
        "-include missing.mk\n"
        "build.all lint: test\n\t@echo ok\n"
        "dist/app.js:\n\ttouch $@\n"
        "VAR := a: b\n"
        "OTHER ::= c\n"
        "test:\n\techo 'recipe: line'\n"
        "multi \\\n  second:\n\ttrue\n"
        "%.o: %.c\n\ttrue\n"
    )
    repo_dir = _repo_with_makefile(
        tmp_path, monkeypatch, mk, **{"common.mk": "shared:\n\ttrue\n"}
    )
    for ok in ("make lint", "make build.all", "make dist/app.js", "make shared"):
        assert validate_command(ok, repo_dir)[0], ok
    for bad in ("make VAR", "make OTHER", "make recipe", "make missing"):
        assert not validate_command(bad, repo_dir)[0], bad


def test_makefile_include_with_traversal_adds_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    repo_dir = _repo_with_makefile(
        tmp_path, monkeypatch, "include ../x.mk\ninclude $(X)\ntest:\n\ttrue\n"
    )
    assert validate_command("make test", repo_dir)[0]


def test_shorthand_runner_ignores_parent_package_json(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", str(tmp_path / "bl"))
    reset_profile_cache()
    repo_dir = tmp_path / "repo"
    (repo_dir / "pkg").mkdir(parents=True)
    _git(repo_dir, "init")
    (repo_dir / "pkg" / "package.json").write_text(
        json.dumps({"scripts": {"test": "true"}})
    )
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "base")
    record_base_sha(repo_dir)
    (repo_dir / "pkg" / "package.json").unlink()
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"echo": "pwn"}}))
    reset_profile_cache()
    for cmd in ("yarn echo hi", "pnpm echo hi", "bun echo hi", "bun run echo hi"):
        assert not validate_command(cmd, repo_dir / "pkg")[0], cmd


@pytest.mark.parametrize(
    "cmd", ["pnpm exec echo hi", "yarn dlx echo hi", "bun x echo hi", "bun run"]
)
def test_runner_subcommands_stay_allowed(repo: Path, cmd: str) -> None:
    assert validate_command(cmd, repo)[0], cmd
