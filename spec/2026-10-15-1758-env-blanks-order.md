---
status: approved
issue: 1758
intent: intent/2026-10-15-1758-env-blanks-order.md
---

# Spec: pin the order of agent env blanks and the MCP env merge

## Design

One behavioural test in a new file `tests/test_env_blanks_mcp_order.py`. It
drives the real `create_client` the way `tests/test_remote_control_wiring.py:32-44`
does (patch `core.client.ClaudeSDKClient` to capture the options, patch
`core.client.require_auth_token`), plus:

- `core.client.get_required_mcp_servers` returns `["github"]`;
- `agents.tools_pkg.mcp_catalog.get_catalog_entry` returns a stub entry
  (`credential_provider=None`) whose `build_server_config` returns
  `{"command": "x", "env": {"GITHUB_TOKEN": <operator value>}}`. It is
  imported inside `create_client` (`client.py:898`), so patching the module
  attribute reaches it.

Host env (monkeypatch): `GITHUB_TOKEN=<host value>` and
`DATABASE_URL=<host value>`, both on the deny list.

Assertions on the captured `options.env`:

1. `GITHUB_TOKEN` equals the operator value (MCP merge ran after the blanks).
2. `DATABASE_URL` equals `""` (blanks still applied for keys no MCP server sets).
3. `"env"` is not in `options.mcp_servers["github"]` (the #599 argv guard
   still holds on this path).

Reversing the order at `client.py:615`/`:915` fails assertion 1; removing the
blanking fails assertion 2.

## Alternatives rejected

- **AST/source-order check of line numbers.** Brittle against refactors and
  does not prove runtime behaviour; the behavioural path is cheap here.
- **Unit test of `_externalize_secret_env` alone.** Already covered by
  `tests/test_mcp_secret_env_599.py`; it cannot see the order in `create_client`.

## Risks

None at runtime (test only). The test depends on `create_client` internals
(`get_required_mcp_servers`, lazy import of `get_catalog_entry`); a refactor
of those names breaks the test loudly, which is the point.

## Verification

- `apps/backend/.venv/bin/pytest tests/test_env_blanks_mcp_order.py -q` passes.
- With `client.py` edited to apply the blanks after the MCP merge (scratch
  copy, not committed), the test fails.
- `ruff check` and `ruff format --check` on the new file pass.
