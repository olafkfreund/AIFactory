# ruff: noqa: ARG001, S105, S603, S607, PLW1510
"""Child processes must not inherit the web server's secrets (#1680)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from server.utils.subprocess_env import (  # noqa: E402
    GITHUB_KEEP,
    child_env,
    make_subprocess_env,
)

SECRETS = (
    "DATABASE_URL",
    "JWT_SECRET",
    "API_TOKEN",
    "AIFACTORY_TOKEN",
    "AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY",
    "S3_SECRET_KEY",
    "APP_OIDC_CLIENT_SECRET",
    "GITHUB_TOKEN",
)


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(k, raising=False)
    for k in SECRETS:
        monkeypatch.setenv(k, "s3cret")
    monkeypatch.setenv("OPENAI_API_KEY", "oa")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/root"))


def test_child_env_drops_secrets(secret_env: None) -> None:
    env = child_env()
    for k in SECRETS:
        assert k not in env, f"leaked {k}"


def test_child_env_keeps_path_and_home(secret_env: None) -> None:
    env = child_env()
    assert env["PATH"] == os.environ["PATH"]
    assert env["HOME"] == os.environ["HOME"]


def test_keep_restores_only_named(secret_env: None) -> None:
    env = child_env(keep=GITHUB_KEEP)
    assert env["GITHUB_TOKEN"] == "s3cret"
    assert "DATABASE_URL" not in env


def test_hooks_path_appended_after_existing_entry(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.https://github.com.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!gh auth git-credential")
    env = child_env()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_1"] == "/dev/null"


def test_hooks_path_is_entry_zero_without_count(secret_env: None) -> None:
    env = child_env()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"


def test_make_subprocess_env_keep_and_drop(secret_env: None) -> None:
    env = make_subprocess_env()
    assert env["OPENAI_API_KEY"] == "oa"
    assert env["GITHUB_TOKEN"] == "s3cret"
    assert "DATABASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_make_subprocess_env_can_keep_anthropic_key(secret_env: None) -> None:
    env = make_subprocess_env(strip_anthropic_api_key=False)
    assert env["ANTHROPIC_API_KEY"] == "an"


def test_real_git_hook_disabled_by_child_env(secret_env: None, tmp_path: Path) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    base = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=base)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    cmd = [*git, "-C", str(tmp_path), "commit", "--allow-empty", "-qm", "x"]
    assert subprocess.run(cmd, env=base, capture_output=True).returncode != 0
    scrubbed = {**child_env(), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    assert subprocess.run(cmd, env=scrubbed, capture_output=True).returncode == 0
