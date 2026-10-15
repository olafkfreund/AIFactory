---
status: approved
issue: 1741
spec: spec/2026-10-15-1741-simple-client-tools-allowlist.md
---

# Plan: simple clients get only the tools their helper needs

Self-contained. You do not need to open the intent or the spec.

**Decision.** `create_simple_client` runs under `bypassPermissions`, so
`allowed_tools` restricts nothing and the CLI offers its default tools. Pass the
helper's `AGENT_CONFIGS` list (already in `allowed_tools`) as the SDK `tools=`
base set too. Text-only helpers (commit_message, merge_resolver,
spec_compaction) get no tools; insights, batch_analysis and
batch_validation get Read/Glob/Grep only (step 2). Keep the #1719
`disallowed_tools`. The pinned SDK (`requirements.lock:671`,
claude-agent-sdk 0.2.164) supports `tools` (`[]` → `--tools ""`).

## Steps

1. `apps/backend/core/simple_client.py:112`: after
   `allowed_tools=allowed_tools,` add
   `tools=allowed_tools,  # #1741: base set; allowed_tools alone does not limit under bypass`.
   `tests/test_command_sandbox.py` after
   `test_simple_client_options_disallow_write_tools` (ends `:664`): add a
   parametrized `test_simple_client_options_limit_base_tools(agent_type,
   monkeypatch)` with the same setup, asserting
   `seen["options"].tools == AGENT_CONFIGS[agent_type]["tools"]`.
   → verify by `apps/backend/.venv/bin/pytest tests/test_command_sandbox.py -q -k simple_client`
   Traps: do not reformat other lines; `AGENT_CONFIGS` is already imported in
   the test file.

2. Deviation from the spec, decided by the lead on review: `insights`
   (reads the session git diff) and `batch_analysis` (reads GitHub issue
   bodies; its prompt says "Do NOT use any tools") also lose WebSearch and
   WebFetch, because Read plus WebFetch is the exfiltration pair this issue
   closes. No other code path uses these two types: the chat runner
   (`runners/insights_runner.py:190`) sets its own Read/Glob/Grep list.
   - `apps/backend/agents/tools_pkg/models.py:233` and `:279`:
     `BASE_READ_TOOLS + WEB_TOOLS` → `BASE_READ_TOOLS`.
   - `apps/frontend-web/src/components/AgentTools.tsx` (`insights`,
     `batch_analysis` entries): the static mirror of `models.py` drops
     WebFetch/WebSearch.
   - `tests/test_command_sandbox.py`: the step 1 test also asserts the
     property directly: `tools == []` for the text-only helpers, and no
     WebFetch/WebSearch in a non-empty list for the read helpers.
   → verify by `apps/backend/.venv/bin/pytest tests/test_command_sandbox.py tests/test_agent_configs.py -q`
   Traps: do not touch `analysis`/`pr_*` configs; they are full create_client agents.

## Tests

- Regression proof: the new test fails against `origin/dev`'s
  `simple_client.py` (`tools` is `None`) and passes after.
- `apps/backend/.venv/bin/pytest tests/test_command_sandbox.py tests/test_agent_env_scrub.py tests/test_outbound_scrub_all_providers.py -q`.
- `ruff check` and `ruff format --check` on both files.

## Rollback

Revert the commit; the #1719 `disallowed_tools` still blocks writes.
