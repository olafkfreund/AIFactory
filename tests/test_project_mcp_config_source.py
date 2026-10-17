"""Tests for load_project_mcp_config source selection (#1795)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.client import load_project_mcp_config

ENV_VAR = "AIFACTORY_PROJECT_MCP_CONFIG"

VALID_NPX_SERVER = {
    "id": "custom-npx",
    "name": "Custom NPX Server",
    "type": "command",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-everything"],
}


def _write_env_file(project_dir: Path, content: str) -> Path:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / ".env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def test_empty_env_var_ignores_project_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) #1791 regression: env var is '{}' and .aifactory/.env has CUSTOM_MCP_SERVERS -> {}."""
    monkeypatch.setenv(ENV_VAR, "{}")
    _write_env_file(
        tmp_path,
        f"CUSTOM_MCP_SERVERS={json.dumps([VALID_NPX_SERVER])}\n",
    )

    assert load_project_mcp_config(tmp_path) == {}


def test_env_var_loads_toggle_and_valid_custom_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) Variable holds CONTEXT7_ENABLED=false and one valid custom server; both returned."""
    payload = {
        "CONTEXT7_ENABLED": "false",
        "CUSTOM_MCP_SERVERS": json.dumps([VALID_NPX_SERVER]),
    }
    monkeypatch.setenv(ENV_VAR, json.dumps(payload))

    result = load_project_mcp_config(tmp_path)

    assert result == {
        "CONTEXT7_ENABLED": "false",
        "CUSTOM_MCP_SERVERS": [VALID_NPX_SERVER],
    }


def test_invalid_json_in_env_var_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) Invalid JSON in AIFACTORY_PROJECT_MCP_CONFIG gives {}."""
    monkeypatch.setenv(ENV_VAR, "{not-valid-json")
    _write_env_file(tmp_path, "CONTEXT7_ENABLED=false\n")

    assert load_project_mcp_config(tmp_path) == {}


def test_unset_env_var_reads_file_for_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) Variable is unset and the file is read (the CLI case)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    _write_env_file(
        tmp_path,
        f"CONTEXT7_ENABLED=false\nCUSTOM_MCP_SERVERS={json.dumps([VALID_NPX_SERVER])}\n",
    )

    result = load_project_mcp_config(tmp_path)

    assert result == {
        "CONTEXT7_ENABLED": "false",
        "CUSTOM_MCP_SERVERS": [VALID_NPX_SERVER],
    }
