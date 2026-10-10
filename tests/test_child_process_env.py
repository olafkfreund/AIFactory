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
    "KMS_FERNET_KEY",
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
    scrubbed = {
        **child_env(),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    assert subprocess.run(cmd, env=scrubbed, capture_output=True).returncode == 0


# --- backend in-process helpers (#1680 review fix) ---------------------------


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r.git", "https://example.com/o/r.git"]
)
@pytest.mark.parametrize("token", ["tok", ""])
def test_authed_push_url_env_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, url: str, token: str
) -> None:
    from core.git_credentials import authed_push_url

    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    with authed_push_url(url) as (_, env):
        assert "DATABASE_URL" not in env
        assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"
        # the token only travels via the askpass pair, never as GITHUB_TOKEN
        assert "GITHUB_TOKEN" not in env
        assert ("GIT_PASS" in env) == bool(
            token and url.startswith("https://github.com/")
        )


def test_core_child_env_extra_count_appends_hooks_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.child_env import child_env as core_child_env

    env = core_child_env(
        extra={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "x",
        }
    )
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_1"] == "/dev/null"


def test_opt_in_keeps_both_anthropic_credentials(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", "/run/key")
    env = make_subprocess_env(strip_anthropic_api_key=False)
    assert env["ANTHROPIC_API_KEY_FILE"] == "/run/key"
    assert "ANTHROPIC_API_KEY_FILE" not in make_subprocess_env()


def test_trusted_plan_git_env_is_scrubbed_and_headless(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trusted_plan  # noqa: PLC0415

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    env = trusted_plan._git_subprocess_env()
    assert env is not None
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "DATABASE_URL" not in env
    assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"


def test_credential_names_dropped_for_tools_kept_for_runner(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CONTEXT7_KEY",
        "S3_ACCESS_KEY",
        "OLLAMA_API_KEY",
    ):
        monkeypatch.setenv(k, "c")
    tools = child_env()
    for k in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CONTEXT7_KEY",
        "S3_ACCESS_KEY",
        "OLLAMA_API_KEY",
    ):
        assert k not in tools, f"leaked {k}"
    assert (
        child_env(keep=("CLAUDE_CODE_OAUTH_TOKEN",))["CLAUDE_CODE_OAUTH_TOKEN"] == "c"
    )
    runner = make_subprocess_env()
    assert runner["CLAUDE_CODE_OAUTH_TOKEN"] == "c"
    assert runner["CONTEXT7_KEY"] == "c"
    assert "DATABASE_URL" not in runner


def test_github_app_private_key_never_reaches_a_child(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The App key is popped at boot; the deny pattern is the second layer (#1671)."""
    name = "AIFACTORY_GITHUB_APP_PRIVATE_KEY"
    monkeypatch.setenv(name, "-----BEGIN RSA PRIVATE KEY-----x")
    assert name not in child_env()
    assert name not in child_env(keep=GITHUB_KEEP)
    assert name not in make_subprocess_env()
    assert name not in make_subprocess_env(strip_anthropic_api_key=False)
