#!/usr/bin/env python3
"""
Agent env credential scrubbing (#363 H1 slice 1 — epic #318)
============================================================

The Claude Agent SDK spawns the agent CLI with the full host environment merged
in, so without scrubbing the agent's Bash subprocess inherits every host secret
(AIFactory `API_TOKEN`/`JWT_SECRET`, `DATABASE_URL`, cloud/Vault creds, provider
keys) — exfiltratable via `env`/`printenv`. `get_agent_env_blanks` neutralizes
them (empty value wins over the inherited one) while preserving the agent's own
auth vars.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "backend"))

from core.auth import get_agent_env_blanks  # noqa: E402

_GIT = shutil.which("git")  # resolved before clean_env narrows PATH

SECRETS = {
    "API_TOKEN": "x",
    "APP_API_TOKEN": "x",
    "JWT_SECRET": "x",
    "APP_JWT_SECRET": "x",
    "DATABASE_URL": "postgres://u:p@h/db",
    "AWS_SECRET_ACCESS_KEY": "x",
    "AWS_ACCESS_KEY_ID": "x",
    "AWS_SESSION_TOKEN": "x",
    "VAULT_TOKEN": "x",
    "AZURE_CLIENT_SECRET": "x",
    "OPENAI_API_KEY": "x",
    "ANTHROPIC_API_KEY": "x",
    "GITHUB_TOKEN": "x",
    # generic-pattern matches
    "MY_DB_PASSWORD": "x",
    "SERVICE_PRIVATE_KEY": "x",
    "FOO_CREDENTIAL": "x",
    "BAR_KMS_KEY": "x",
    "SSH_PASSPHRASE": "x",
    # trusted-plan HMAC keys, legacy and keyed forms (#1667)
    "AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY": "x",
    "AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY__2026Q3": "x",
    "AIFACTORY_TOKEN": "x",
    # Host keys that matched neither list (#1674)
    "APP_CFACTORY_READ_KEY": "x",
    "CONTEXT7_KEY": "x",
    "RAPIDAPI_KEY": "x",
    "LANGCHAIN_API_KEY": "x",
    "OPENAI_COMPATIBLE_API_KEY": "x",
    "S3_ACCESS_KEY": "x",
    "OLLAMA_API_KEY": "x",
    # verdict digest; blank reads as hold in the agent (#1708)
    "AIFACTORY_TRUSTED_CONTRACT": "x",
}

KEEP = {
    "CLAUDE_CODE_OAUTH_TOKEN": "tok",
    "ANTHROPIC_AUTH_TOKEN": "tok",
    "ANTHROPIC_BASE_URL": "https://x",
    "ANTHROPIC_MODEL": "claude",
    "PATH": "/usr/bin",
    "LANG": "en_US",
}


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    # Start from a controlled environment.
    for k in list(os.environ):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**SECRETS, **KEEP}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_blanks_keep_git_config_pins(clean_env, monkeypatch, tmp_path):
    """The SDK env keeps the server's git pins, so repo hooks stay off (#1757)."""
    import subprocess

    from core.child_env import pin_git_config

    if not _GIT:
        pytest.skip("git not installed")
    pins = pin_git_config({})
    for k, v in pins.items():
        monkeypatch.setenv(k, v)  # undone after the test; raw os.environ leaks
    blanks = get_agent_env_blanks()
    assert not set(pins) & set(blanks), sorted(set(pins) & set(blanks))

    repo = tmp_path / "repo"
    subprocess.run([_GIT, "init", "-q", str(repo)], check=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho HOOK-RAN >&2\nexit 1\n")
    hook.chmod(0o755)
    env = {
        **os.environ,
        **blanks,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    r = subprocess.run(
        [_GIT, "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "HOOK-RAN" not in r.stderr, r.stderr


def test_all_secrets_are_blanked(clean_env):
    blanks = get_agent_env_blanks()
    for key in SECRETS:
        assert key in blanks, f"secret not scrubbed: {key}"
        assert blanks[key] == "", f"secret not blanked to empty: {key}"


def test_agent_and_benign_vars_are_kept(clean_env):
    blanks = get_agent_env_blanks()
    for key in KEEP:
        assert key not in blanks, f"wrongly scrubbed a needed var: {key}"


def test_devshell_browser_vars_are_kept(clean_env, monkeypatch):
    """#1757 review: a nix devShell's Playwright wiring must reach the agent."""
    for key in (
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
        "PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS",
        "FONTCONFIG_FILE",
    ):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("PLAYWRIGHT_SERVICE_ACCESS_TOKEN", "x")
    blanks = get_agent_env_blanks()
    assert not {k for k in blanks if k.startswith(("PLAYWRIGHT_SKIP", "FONTCONFIG"))}
    assert "PLAYWRIGHT_BROWSERS_PATH" not in blanks
    assert "PLAYWRIGHT_SERVICE_ACCESS_TOKEN" in blanks


def test_allowlist_blanks_unknown_names_and_honors_safe_extra(clean_env, monkeypatch):
    for name in ("SSH_AUTH_SOCK", "FOO_SECRETISH", "PWD", "NODE_ENV", "MY_TOOL_DIR"):
        monkeypatch.setenv(name, "x")
    for name in ("LC_ALL", "NIX_PATH", "https_proxy"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("AIFACTORY_AGENT_ENV_EXTRA", "MY_TOOL_DIR,JWT_SECRET")

    blanks = get_agent_env_blanks()
    for name in (
        "SSH_AUTH_SOCK",
        "FOO_SECRETISH",
        "HOME",
        "PWD",
        "NODE_ENV",
        "JWT_SECRET",
    ):
        assert blanks[name] == ""
    for name in (
        "PATH",
        "LANG",
        "LC_ALL",
        "NIX_PATH",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "https_proxy",
        "MY_TOOL_DIR",
    ):
        assert name not in blanks


def test_dropped_names_logged_once_without_values(clean_env, monkeypatch, caplog):
    from core import auth
    from core.child_env import child_env

    monkeypatch.setattr(auth, "_logged_dropped_env_names", set())
    monkeypatch.setenv("FOO_MISC", "private-marker")
    monkeypatch.setenv("FOO_SECRETISH", "private-marker")
    with caplog.at_level(logging.INFO, logger="core.auth"):
        get_agent_env_blanks()
        get_agent_env_blanks()
        child_env(agent=True)

    messages = [r.message for r in caplog.records if "#1757" in r.message]
    assert len(messages) == 1
    assert "FOO_MISC" in messages[0]
    assert "FOO_SECRETISH" not in messages[0]
    assert "private-marker" not in messages[0]


def test_oauth_token_never_blanked(clean_env, monkeypatch):
    # Even though it ends in _TOKEN, the agent's own auth must survive.
    blanks = get_agent_env_blanks()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in blanks
    assert "ANTHROPIC_AUTH_TOKEN" not in blanks


def test_empty_when_no_secrets(monkeypatch):
    for k in list(os.environ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert get_agent_env_blanks() == {}


def test_lowercase_and_mixed_case_denied_names_are_blanked(clean_env, monkeypatch):
    monkeypatch.setenv("app_api_token", "x")
    monkeypatch.setenv("Ollama_Api_Key", "x")
    blanks = get_agent_env_blanks()
    assert blanks["app_api_token"] == ""
    assert blanks["Ollama_Api_Key"] == ""


def test_deny_exact_entries_are_upper_case():
    from core.auth import _AGENT_ENV_DENY_EXACT

    assert all(k == k.upper() for k in _AGENT_ENV_DENY_EXACT)


def test_simple_client_env_is_scrubbed(clean_env, monkeypatch, tmp_path):
    """create_simple_client must blank host secrets like create_client does."""
    from core import simple_client

    seen = {}

    class _Client:
        pass

    def _capture(**kw):
        seen["env"] = kw["options"].env
        return _Client()

    monkeypatch.setattr(simple_client, "require_auth_token", lambda: "tok")
    monkeypatch.setattr(simple_client, "ClaudeSDKClient", _capture)
    monkeypatch.setattr(simple_client, "wrap_client_outbound_scrub", lambda c, *_: c)
    client = simple_client.create_simple_client(
        agent_type="commit_message", model="claude-haiku-4-5", cwd=tmp_path
    )
    assert Path(seen["env"]["HOME"]).name.startswith("aif-home-")
    assert Path(seen["env"]["HOME"]).is_dir()
    for key in SECRETS:
        assert seen["env"].get(key) == "", f"simple client inherits {key}"
    del client
