---
status: draft
issue: 1758
spec: spec/2026-10-15-1758-env-blanks-order.md
---

# Plan: pin the order of agent env blanks and the MCP env merge

Self-contained. Test only; no production code changes.

**Decision.** In `create_client` (`apps/backend/core/client.py`) the host
secret blanks are applied at `:615` (`sdk_env.update(get_agent_env_blanks())`)
and catalog MCP server env is merged over them at `:915`
(`_externalize_secret_env(cfg, sdk_env)`, defined `:380-394`). That order is
intended: operator MCP credentials such as `GITHUB_TOKEN` must survive. The
test pins it behaviourally.

## Steps

1. `tests/test_env_blanks_mcp_order.py` (new): one test,
   `test_mcp_env_merges_over_agent_blanks(tmp_path, monkeypatch)`:
   - `monkeypatch.setenv("GITHUB_TOKEN", host)`, `setenv("DATABASE_URL", host)`.
   - stub entry: `SimpleNamespace(id="github", credential_provider=None,
     build_server_config=lambda creds, read_only: {"command": "x", "env":
     {"GITHUB_TOKEN": operator}})`.
   - patch `core.client.ClaudeSDKClient` (capture `options`, return
     `MagicMock()`), `core.client.require_auth_token` (return
     `"sk-ant-oat01-test"`), `core.client.get_required_mcp_servers` (return
     `["github"]`), `agents.tools_pkg.mcp_catalog.get_catalog_entry` (return
     the stub).
   - call `create_client(project_dir=tmp_path, spec_dir=<tmp_path/"001">,
     model="claude-sonnet-4-5-20250929", agent_type="coder")`.
   - assert `options.env["GITHUB_TOKEN"] == operator`,
     `options.env["DATABASE_URL"] == ""`,
     `"env" not in options.mcp_servers["github"]`.
   - `sys.path.insert(0, <repo>/apps/backend)` as the other tests do.
   → verify by `apps/backend/.venv/bin/pytest tests/test_env_blanks_mcp_order.py -q`
   Traps: use test-only, non-token-shaped values (no `ghp_` prefix) so the
   secret scanner stays quiet; `spec_dir` must exist.

## Tests

- `apps/backend/.venv/bin/pytest tests/test_env_blanks_mcp_order.py tests/test_mcp_secret_env_599.py tests/test_agent_sdk_env_blanks.py -q`: all pass.
- Mutation proof: in a scratch copy, move the `get_agent_env_blanks()` update
  below the MCP merge; the new test fails on the `GITHUB_TOKEN` assert.
- `ruff check tests/test_env_blanks_mcp_order.py` and `ruff format --check` pass.

## Rollback

Delete the test file.
