"""#599: catalog MCP server credentials must NOT land in the --mcp-config argv.

The claude-agent-sdk serialises the mcpServers dict (incl. each server's env
values) into a `--mcp-config <json>` argv visible via `ps aux`. _externalize_secret_env
moves the credential into the process env instead, so nothing secret reaches the
command line; the MCP subprocess inherits it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from core.client import _externalize_secret_env  # noqa: E402

_TOKEN = "ghp_TESTONLY_not_a_real_token"


def test_env_moved_out_of_server_config():
    sink: dict = {}
    cfg = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": _TOKEN},
    }
    out = _externalize_secret_env(cfg, sink)
    assert "env" not in out  # nothing secret left in the server config
    assert sink["GITHUB_PERSONAL_ACCESS_TOKEN"] == _TOKEN  # inherited via process env


def test_token_absent_from_mcp_config_argv():
    sink: dict = {}
    cfg = _externalize_secret_env(
        {
            "command": "npx",
            "args": ["x"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": _TOKEN},
        },
        sink,
    )
    # This is exactly what the SDK json.dumps into the --mcp-config argv.
    serialized = json.dumps({"mcpServers": {"github": cfg}})
    assert _TOKEN not in serialized, "token must never reach the --mcp-config argv"


def test_noop_without_env():
    sink: dict = {}
    cfg = {"command": "npx", "args": ["x"]}
    assert _externalize_secret_env(cfg, sink) == {"command": "npx", "args": ["x"]}
    assert sink == {}


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f()
            print("ok:", n)
    print("mcp secret-env #599 tests: passed")
