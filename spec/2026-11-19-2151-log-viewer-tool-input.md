---
status: draft
issue: 2151
intent: intent/2026-11-19-2151-log-viewer-tool-input.md
---

# Spec: Task log viewer shows tool input as null

## Design

New module `apps/backend/task_logger/tool_input.py` with one function `describe_tool_input(inp) -> str | None`, exported from `task_logger`:

- not a non-empty dict -> `None`.
- the first of `command`, `pattern` (shown as `pattern: <v>`), `file_path`, `path`, `url`, `query` whose value is a non-empty string gives the text; any other dict (including one whose preferred value is not a string) becomes compact JSON (`json.dumps(..., default=str, separators)`), with the values of keys whose lowercase name contains `password`, `token`, `secret`, `key`, `authorization` or `credential` replaced by `***REDACTED***` (recursively).
- A public `scrub(text)` in the same module removes whole PEM private-key blocks (same regex as `transcript.py`), then applies `factory_common.secrets.redact`. The text goes through `scrub` first, then shortened to 100 characters (the same cap `TaskLogger.tool_start` already applies); long `file_path` keeps its tail (`...` + last chars) as today.

The five call sites (`qa/reviewer.py`, `qa/fixer.py`, `agents/session.py`, `task_logger/capture.py`, `spec/pipeline/agent_runner.py`) drop their private key ladders and call the helper. `TaskLogger.tool_start` additionally runs `scrub` on its argument before truncating, so any other caller is covered. The raw-input paths (`session.py` debug `full_input=str(inp)[:500]` and the verbose `Input:` prints in `session.py`, `qa/reviewer.py`, `qa/fixer.py`) are switched to the scrubbed display string so debug/verbose output does not defeat the redaction. No web-server or frontend change: stored logs already pass `tool_input`/`input` through.

## Alternatives rejected

- Add the missing `command` branch to the reviewer only: fixes Bash but leaves MCP/ToolSearch null and keeps five diverging copies.
- Log the full input dict: bloats task_logs.json and the stream, and widens the secret surface.
- Redact only in `tool_start`: cannot mask secret-named JSON keys, and shortening at the call site could cut a token before redaction.

## Risks

The compact-JSON fallback shows more than before (previously nothing), so redaction is the risk: key-name masking plus `redact` before truncation. A secret under an innocuous key name in a non-token shape still shows; accepted, same limit as the transcript redactor. Entries are slightly longer (100 not 50 chars) in the viewer.

## Verification

Unit tests in `apps/backend/task_logger/test_tool_input.py`: Bash command, MCP dict falls back to JSON, secret-named key masked, token in a command redacted before truncation, empty input gives None. A direct `TaskLogger.tool_start` test with a token straddling the 100-char boundary, asserting `content`, `tool_input` and the emitted marker are redacted; PEM block, nested dict/list and non-string preferred-value cases. A test through the QA reviewer path (Bash block yields a non-null `tool_input` in the logger entry), which fails on the old code. Run `pytest apps/backend/task_logger apps/backend/qa apps/backend/agents -q` and ruff check/format. Live: new task logs show tool inputs after deploy.
