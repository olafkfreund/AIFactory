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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "backend"))

from core.auth import get_agent_env_blanks  # noqa: E402

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
}

KEEP = {
    "CLAUDE_CODE_OAUTH_TOKEN": "tok",
    "ANTHROPIC_AUTH_TOKEN": "tok",
    "ANTHROPIC_BASE_URL": "https://x",
    "ANTHROPIC_MODEL": "claude",
    "PATH": "/usr/bin",
    "HOME": "/home/x",
    "LANG": "en_US",
    "PWD": "/work",
    "NODE_ENV": "production",
}


@pytest.fixture
def clean_env(monkeypatch):
    # Start from a controlled environment.
    for k in list(__import__("os").environ):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**SECRETS, **KEEP}.items():
        monkeypatch.setenv(k, v)


def test_all_secrets_are_blanked(clean_env):
    blanks = get_agent_env_blanks()
    for key in SECRETS:
        assert key in blanks, f"secret not scrubbed: {key}"
        assert blanks[key] == "", f"secret not blanked to empty: {key}"


def test_agent_and_benign_vars_are_kept(clean_env):
    blanks = get_agent_env_blanks()
    for key in KEEP:
        assert key not in blanks, f"wrongly scrubbed a needed var: {key}"


def test_oauth_token_never_blanked(clean_env, monkeypatch):
    # Even though it ends in _TOKEN, the agent's own auth must survive.
    blanks = get_agent_env_blanks()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in blanks
    assert "ANTHROPIC_AUTH_TOKEN" not in blanks


def test_empty_when_no_secrets(monkeypatch):
    for k in list(__import__("os").environ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert get_agent_env_blanks() == {}
