"""Tests for the RFC-0013 operator-declared MCP catalog entries (#644).

Operator servers from ~/.aifactory/mcp-servers.json surface through
``operator_catalog_entries()`` and override built-ins by id; the deployment-metrics
MCP is operator-supplied (not an auto-launched built-in default).
"""

from __future__ import annotations

import json
from pathlib import Path

import core.mcp_credentials as mc
import pytest
from agents.tools_pkg import mcp_catalog
from core.mcp_credentials import CredentialStatus


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mc, "OPERATOR_MCP_SERVERS_PATH", home / "mcp-servers.json")
    monkeypatch.setattr(mc, "OPERATOR_CONFIG_PATH", home / "mcp-credentials.json")
    mc.reset_cache()
    yield
    mc.reset_cache()


def _write_servers(payload: dict) -> None:
    path = mc.OPERATOR_MCP_SERVERS_PATH
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def test_no_operator_config_no_extra_entries():
    assert mcp_catalog.operator_catalog_entries() == {}
    # deployment-metrics is NOT a built-in auto-launched default.
    assert "deployment-metrics" not in [e.id for e in mcp_catalog.CATALOG]


def test_operator_stdio_entry_materialized():
    _write_servers(
        {
            "servers": {
                "deployment-metrics": {
                    "command": "datadog-mcp",
                    "args": ["serve"],
                    "agents": ["planner", "coder"],
                    "docs_url": "https://dora.example/docs",
                }
            }
        }
    )
    entries = mcp_catalog.operator_catalog_entries()
    assert "deployment-metrics" in entries
    entry = entries["deployment-metrics"]
    assert entry.launcher_command == "datadog-mcp"
    assert entry.launcher_args == ["serve"]
    assert entry.default_for_agents == ["planner", "coder"]
    # credential_provider defaults to the id with dashes normalized.
    assert entry.credential_provider == "deployment_metrics"


def test_operator_entry_visible_through_lookup():
    _write_servers({"servers": {"deployment-metrics": {"command": "datadog-mcp"}}})
    assert mcp_catalog.is_catalog_server("deployment-metrics") is True
    assert mcp_catalog.get_catalog_entry("deployment-metrics") is not None
    assert "deployment-metrics" in mcp_catalog.catalog_ids()


def test_operator_overrides_builtin_by_id():
    # An operator may override a built-in (e.g. github) by id.
    _write_servers({"servers": {"github": {"command": "my-github-mcp", "args": ["--x"]}}})
    entry = mcp_catalog.get_catalog_entry("github")
    assert entry.launcher_command == "my-github-mcp"


def test_secrets_resolved_into_config_env_not_argv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DORA_TOKEN", "topsecret")
    _write_servers(
        {
            "servers": {
                "deployment-metrics": {
                    "command": "datadog-mcp",
                    "args": ["serve"],
                    "credential_provider": "deployment_metrics",
                    "env": {"DATADOG_API_KEY": "DORA_TOKEN"},
                }
            }
        }
    )
    entry = mcp_catalog.get_catalog_entry("deployment-metrics")
    creds = CredentialStatus(
        available=True, source="operator-config", env_vars={"DATADOG_API_KEY": "topsecret"}
    )
    config = entry.build_server_config(creds, read_only=True)
    # Secret lands in env, never on argv.
    assert config["env"] == {"DATADOG_API_KEY": "topsecret"}
    assert "topsecret" not in json.dumps(config["args"])


def test_http_operator_entry():
    _write_servers(
        {
            "servers": {
                "remote-dora": {
                    "transport": "http",
                    "url": "https://dora.example/mcp",
                }
            }
        }
    )
    entry = mcp_catalog.get_catalog_entry("remote-dora")
    assert entry.transport == "http"
    config = entry.build_server_config(None)
    assert config == {"type": "http", "url": "https://dora.example/mcp"}


def test_builtins_still_present():
    # Back-compat: the V1 built-ins remain.
    ids = mcp_catalog.catalog_ids()
    for builtin in ("github", "kubernetes", "aws", "azure"):
        assert builtin in ids
