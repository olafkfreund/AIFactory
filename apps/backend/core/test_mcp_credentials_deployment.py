"""Tests for the RFC-0013 operator MCP-server config + deployment-metrics probe (#644).

Covers ~/.aifactory/mcp-servers.json loading (perm-safe, env-resolved, no secrets
in argv), the deployment_metrics credential probe (honest: available only when an
operator server is declared), and back-compat with the existing probes.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import core.mcp_credentials as mc
import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the operator config paths at a tmp dir and reset caches."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mc, "OPERATOR_MCP_SERVERS_PATH", home / "mcp-servers.json")
    monkeypatch.setattr(mc, "OPERATOR_CONFIG_PATH", home / "mcp-credentials.json")
    mc.reset_cache()
    yield
    mc.reset_cache()


def _write_servers(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(payload))
    path.chmod(mode)


def test_no_config_returns_empty():
    assert mc.get_operator_mcp_servers() == {}
    assert mc.get_credential_status("deployment_metrics").available is False


def test_declared_stdio_server_is_returned():
    _write_servers(
        mc.OPERATOR_MCP_SERVERS_PATH,
        {
            "servers": {
                "deployment-metrics": {
                    "command": "python3",
                    "args": ["/opt/provider/server.py"],
                    "agents": ["planner", "coder"],
                }
            }
        },
    )
    servers = mc.get_operator_mcp_servers()
    assert "deployment-metrics" in servers
    status = mc.get_credential_status("deployment_metrics")
    assert status.available is True
    assert status.source == "operator-config"


def test_env_is_resolved_from_environment_not_stored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MY_DORA_TOKEN", "s3cr3t")
    _write_servers(
        mc.OPERATOR_MCP_SERVERS_PATH,
        {
            "servers": {
                "deployment-metrics": {
                    "command": "datadog-mcp",
                    "args": ["serve"],
                    "env": {"DATADOG_API_KEY": "MY_DORA_TOKEN"},
                }
            }
        },
    )
    status = mc.get_credential_status("deployment_metrics")
    assert status.env_vars == {"DATADOG_API_KEY": "s3cr3t"}
    # The secret value must NOT appear in the args (no secrets in argv).
    spec = mc.get_operator_mcp_servers()["deployment-metrics"]
    assert "s3cr3t" not in json.dumps(spec.get("args", []))


def test_unset_env_reference_is_omitted():
    _write_servers(
        mc.OPERATOR_MCP_SERVERS_PATH,
        {
            "servers": {
                "deployment-metrics": {
                    "command": "datadog-mcp",
                    "env": {"DATADOG_API_KEY": "DEFINITELY_UNSET_VAR_XYZ"},
                }
            }
        },
    )
    status = mc.get_credential_status("deployment_metrics")
    assert status.available is True
    assert status.env_vars == {}  # unset reference dropped, degrade not fabricate


def test_malformed_entries_are_skipped():
    _write_servers(
        mc.OPERATOR_MCP_SERVERS_PATH,
        {
            "servers": {
                "no-command": {"args": ["x"]},
                "http-no-url": {"transport": "http"},
                "good": {"command": "foo"},
                "not-a-dict": "nope",
            }
        },
    )
    servers = mc.get_operator_mcp_servers()
    assert set(servers) == {"good"}


def test_loose_permissions_refused():
    path = mc.OPERATOR_MCP_SERVERS_PATH
    _write_servers(path, {"servers": {"x": {"command": "foo"}}}, mode=0o644)
    # group/other-readable => refused.
    assert mc.get_operator_mcp_servers() == {}


def test_http_server_requires_url():
    _write_servers(
        mc.OPERATOR_MCP_SERVERS_PATH,
        {"servers": {"remote": {"transport": "http", "url": "https://dora.example/mcp"}}},
    )
    assert "remote" in mc.get_operator_mcp_servers()


def test_existing_probes_unaffected():
    # Sanity: an unrelated unknown provider still returns unavailable.
    assert mc.get_credential_status("nonsense").available is False


def test_world_readable_mask_matches_0600():
    # Guard the perm check logic itself.
    path = mc.OPERATOR_MCP_SERVERS_PATH
    _write_servers(path, {"servers": {"x": {"command": "foo"}}}, mode=0o600)
    mode = path.stat().st_mode & 0o777
    assert not (mode & (stat.S_IRWXG | stat.S_IRWXO))
    assert mc.get_operator_mcp_servers() == {"x": {"command": "foo"}}
