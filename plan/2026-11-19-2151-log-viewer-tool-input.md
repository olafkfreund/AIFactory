---
status: draft
issue: 2151
spec: spec/2026-11-19-2151-log-viewer-tool-input.md
coders:
  1: claude (lead; new helper plus its tests, security-sensitive)
  2: claude (lead; mechanical swap at five sites, one shared import)
  3: claude (lead)
reviewers:
  - codex (read-only review of the final diff, redaction focus)
---

# Plan: Task log viewer shows tool input as null

Approved decisions (spec): one helper `describe_tool_input` plus public `scrub` in `apps/backend/task_logger/tool_input.py`; preferred string keys `command`, `pattern`, `file_path`, `path`, `url`, `query`, else compact JSON with secret-named keys masked recursively; `scrub` = whole PEM blocks removed then `factory_common.secrets.redact`, applied before shortening to 100 chars; `TaskLogger.tool_start` also scrubs; raw debug/verbose input prints scrubbed; five call sites use the helper; no web-server or frontend change.

## Steps

1. `apps/backend/task_logger/tool_input.py` (new), export both names from `task_logger/__init__.py`; `task_logger/logger.py` `tool_start` (~line 411): `display_input = scrub(tool_input)` before the 100-char truncation. Tests in `apps/backend/task_logger/test_tool_input.py`. Verify: `pytest apps/backend/task_logger -q`.
   Traps: ruff vs standards/ruff.toml disagree on `as` aliases in parenthesised imports, use plain names; no unnecessary lambdas (CodeQL).
2. Replace the key ladders with `describe_tool_input(inp)` in `qa/reviewer.py` (~226), `qa/fixer.py` (~160), `agents/session.py` (~541), `task_logger/capture.py` (~86), `spec/pipeline/agent_runner.py` (`_extract_tool_input_display`, keep the static method delegating). Verify: `pytest apps/backend/qa apps/backend/agents apps/backend/spec -q`.
3. `session.py:564` `full_input` and the verbose `input_str = str(block.input)` in `session.py`, `reviewer.py`, `fixer.py`: wrap in `scrub(...)` before slicing. Add the reviewer-path test (Bash block gives non-null `tool_input`; fails on old code). Verify: new test; ruff check and format.

## Tests

`cd apps/backend && pytest task_logger qa agents spec -q`; `ruff check` and `ruff format --check` on touched files. Expected: all pass.

## Rollback

Revert the commit; log format is unchanged (the field was already optional).
