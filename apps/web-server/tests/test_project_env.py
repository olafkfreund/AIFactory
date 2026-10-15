"""Unit tests for server.services.project_env (#1745)."""

import inspect
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from server.routes import context, github, projects
from server.services import project_env
from server.services.project_env import (
    PROJECT_ENV_KEYS,
    is_project_env_key,
    load_project_env,
)


def test_load_project_env_filters_disallowed_keys(tmp_path: Path) -> None:
    env_dir = tmp_path / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "USE_CLAUDE_MD=true\nLD_PRELOAD=x\nNODE_OPTIONS=x\nBASH_ENV=x\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    load_project_env(tmp_path, env)
    assert env == {"USE_CLAUDE_MD": "true"}


@pytest.mark.parametrize(
    "name",
    [
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXEC_PATH",
        "PATH",
        "HOME",
        "SHELL",
        "HTTPS_PROXY",
        "http_proxy",
    ],
)
def test_refused_keys_never_allowed(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_project_env_key(name) is False
    monkeypatch.setattr(project_env, "PROJECT_ENV_KEYS", PROJECT_ENV_KEYS | {name})
    assert is_project_env_key(name) is False


def test_agent_mcp_pattern() -> None:
    assert is_project_env_key("AGENT_MCP_coder_ADD") is True
    assert is_project_env_key("AGENT_MCP_qa_reviewer_REMOVE") is True
    assert is_project_env_key("AGENT_MCP_") is False
    assert is_project_env_key("AGENT_MCP_coder") is False
    assert is_project_env_key("AGENT_MCP_coder_ADDX") is False


def test_no_override_and_strips_quotes(tmp_path: Path) -> None:
    env_dir = tmp_path / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        'USE_CLAUDE_MD="false"\n'
        "MAIN_BRANCH='develop'\n"
        'AI_FACTORY_MODEL="claude-opus-4-6"\n',
        encoding="utf-8",
    )
    env = {"USE_CLAUDE_MD": "true"}
    load_project_env(tmp_path, env)
    assert env["USE_CLAUDE_MD"] == "true"
    assert env["MAIN_BRANCH"] == "develop"
    assert env["AI_FACTORY_MODEL"] == "claude-opus-4-6"


def test_skipped_warning_logs_key_not_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_dir = tmp_path / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "LD_PRELOAD=sentinel-value-1745\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    with caplog.at_level(logging.WARNING, logger="server.services.project_env"):
        load_project_env(tmp_path, env)

    assert env == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "LD_PRELOAD" in warnings[0]
    assert "sentinel-value-1745" not in warnings[0]


@pytest.mark.parametrize(
    "writer",
    [
        projects.update_project_settings,
        context.update_project_env,
        github._persist_cli_token_to_project,
    ],
)
def test_writer_coverage(writer: Callable[..., Any]) -> None:
    source = inspect.getsource(inspect.unwrap(writer))
    literals = set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', source))
    assert literals
    for key in sorted(literals):
        assert is_project_env_key(key), f"Writer key {key!r} is not in allowlist"


def test_missing_file_is_noop(tmp_path: Path) -> None:
    env = {"EXISTING": "1"}
    load_project_env(tmp_path, env)
    assert env == {"EXISTING": "1"}
