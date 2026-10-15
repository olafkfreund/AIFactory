"""Unit tests for server.services.project_env (#1745)."""

import inspect
import re
from collections.abc import Callable
from typing import Any

import pytest
from server.routes import context, github, projects
from server.services import project_env
from server.services.project_env import (
    PROJECT_ENV_KEYS,
    is_project_env_key,
)


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
