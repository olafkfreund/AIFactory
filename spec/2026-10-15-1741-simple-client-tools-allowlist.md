---
status: draft
issue: 1741
intent: intent/2026-10-15-1741-simple-client-tools-allowlist.md
---

# Spec: simple clients get only the tools their helper needs

## Design

`AGENT_CONFIGS` (`apps/backend/agents/tools_pkg/models.py:123`) already holds
the per-helper list, and `create_simple_client` already reads it into
`allowed_tools` (`apps/backend/core/simple_client.py:95`). Pass the same list
as the SDK's `tools=` option (the base tool set the CLI offers), next to
`allowed_tools` at `simple_client.py:112`. One line; the per-helper lists stay
where they are.

SDK support (checked): `ClaudeAgentOptions.tools: list[str] | ToolsPreset | None`
in claude-agent-sdk 0.2.82 (backend venv) and 0.2.87 (web-server venv);
`subprocess_cli.py:240-250` turns `[]` into `--tools ""` and a list into
`--tools Read,Glob,...`.

Callers and the tool set each gets (from `AGENT_CONFIGS`):

| Caller | agent_type | tools |
| --- | --- | --- |
| `apps/backend/commit_message.py:258` | commit_message | none |
| `apps/backend/core/workspace/merge.py:1456` | merge_resolver | none |
| `apps/backend/merge/ai_resolver/claude_client.py:67` | merge_resolver | none |
| `apps/web-server/server/services/conflict_service.py:477` | merge_resolver | none |
| `apps/web-server/server/services/conflict_service.py:611` | merge_resolver | none |
| `apps/backend/spec/compaction.py:61` | spec_compaction | none |
| `apps/backend/analysis/insight_extractor.py:390` | insights | Read, Glob, Grep, WebSearch, WebFetch |
| `apps/backend/runners/github/batch_issues.py:155` | batch_analysis | Read, Glob, Grep, WebSearch, WebFetch |
| `apps/web-server/server/routes/github.py:182` | batch_analysis | Read, Glob, Grep, WebSearch, WebFetch |
| `apps/backend/runners/github/batch_validator.py:208` | batch_validation | Read, Glob, Grep |

The #1719 `disallowed_tools` stays (defence in depth).

Test: extend the #1719 parametrized test class in
`tests/test_command_sandbox.py` (around `:648-664`, `_SIMPLE_TYPES` at `:474`)
with one test asserting `options.tools == AGENT_CONFIGS[agent_type]["tools"]`
for every simple type. It fails today (`tools` is `None`).

## Alternatives rejected

- **A new `tools=` parameter on `create_simple_client`, set at each caller.**
  Duplicates `AGENT_CONFIGS`, ten call sites to keep in sync.
- **`tools=[]` for every simple client.** Breaks insights and batch_* which
  read the repo.
- **Dropping WebSearch/WebFetch from insights/batch_analysis.** A behaviour
  decision outside this issue; flagged instead.

## Risks

- A helper that silently relied on a default CLI tool not in its list stops
  having it. Checked: the text-only helpers parse a single text answer
  (`max_turns=1`); the read helpers list the tools they use.
- `--tools ""` on an old CLI: the CLI is bundled with the SDK versions above,
  which emit it themselves.

## Verification

- New test fails on `origin/dev`, passes after the change.
- `apps/backend/.venv/bin/pytest tests/test_command_sandbox.py -q` passes.
- `ruff check` / `ruff format --check` on the two changed files.
