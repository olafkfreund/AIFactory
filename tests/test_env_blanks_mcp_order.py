"""Pin the ordering of agent env blanks vs. catalog MCP env merge (#1758)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

import agents.tools_pkg.mcp_catalog as mcp_catalog  # noqa: E402
import core.client as client_mod  # noqa: E402


def test_mcp_env_merges_over_agent_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = "host-ambient-value"
    operator = "operator-mcp-value"
    monkeypatch.setenv("GITHUB_TOKEN", host)
    monkeypatch.setenv("DATABASE_URL", host)

    stub = SimpleNamespace(
        id="github",
        credential_provider=None,
        build_server_config=lambda creds, read_only: {
            "command": "x",
            "env": {"GITHUB_TOKEN": operator},
        },
    )

    captured: dict[str, Any] = {}

    def _fake_sdk_client(options: Any) -> MagicMock:
        captured["options"] = options
        return MagicMock()

    monkeypatch.setattr(client_mod, "ClaudeSDKClient", _fake_sdk_client)
    monkeypatch.setattr(client_mod, "require_auth_token", lambda: "sk-ant-oat01-test")
    monkeypatch.setattr(
        client_mod, "get_required_mcp_servers", lambda *_a, **_kw: ["github"]
    )
    monkeypatch.setattr(mcp_catalog, "get_catalog_entry", lambda _sid: stub)

    spec_dir = tmp_path / "001"
    spec_dir.mkdir()

    client_mod.create_client(
        project_dir=tmp_path,
        spec_dir=spec_dir,
        model="claude-sonnet-4-5-20250929",
        agent_type="coder",
    )

    options = captured["options"]
    assert options.env["GITHUB_TOKEN"] == operator
    assert options.env["DATABASE_URL"] == ""
    assert "env" not in options.mcp_servers["github"]
