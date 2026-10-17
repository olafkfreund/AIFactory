"""Custom MCP credentials travel in the CLI environment, not MCP argv."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from core.client import _custom_server_env, _validate_custom_mcp_server  # noqa: E402


@pytest.mark.parametrize(
    ("env_keys", "valid"),
    [(["MY_MCP_KEY"], True), ("X", False), (["bad-name"], False)],
)
def test_env_keys_validation(env_keys: object, valid: bool) -> None:
    server = {
        "id": "custom",
        "name": "Custom",
        "type": "command",
        "command": "npx",
        "env_keys": env_keys,
    }
    assert _validate_custom_mcp_server(server) is valid


def test_custom_server_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_MCP_KEY", "test-only-value")
    monkeypatch.setenv("JWT_SECRET", "denied-value")
    monkeypatch.delenv("UNSET_MCP_KEY", raising=False)
    custom = {"env_keys": ["MY_MCP_KEY", "JWT_SECRET", "UNSET_MCP_KEY"]}
    sdk_env: dict[str, str] = {}

    _custom_server_env(custom, sdk_env)

    assert sdk_env == {"MY_MCP_KEY": "test-only-value"}
    server = {"command": "npx", "args": []}
    assert "test-only-value" not in json.dumps({"mcpServers": {"custom": server}})
    assert "test-only-value" not in json.dumps(custom)
