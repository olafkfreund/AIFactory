---
status: draft
issue: 2151
author: olafkfreund
---

# Intent: Task log viewer shows tool input as null

## Problem

In the portal task log viewer, many tool-call entries show `"input": null`, so you cannot see what the agent ran. Evidence (factory-lab-rust, 2026-11-18): 59 validation-phase `Bash` TOOL_START markers plus `ToolSearch`, `ScheduleWakeup` and `mcp__aifactory__*` calls have `input: null`; coding-phase `Bash` calls show the command. The display string is derived by hand in five places (`qa/reviewer.py`, `qa/fixer.py`, `agents/session.py`, `task_logger/capture.py`, `spec/pipeline/agent_runner.py`), each with a different key list; the QA reviewer has no `command` branch and every site returns nothing for unknown keys. `TaskLogger.tool_start` also stores the string unredacted.

## Proposed outcome

Every tool-call entry in the log shows a short description of its input (command, path, pattern, or compact JSON for other tools), with secrets redacted.

## Affected users and systems

`apps/backend/task_logger`, the five call sites above; the portal log viewer and the stored `task_logs.json` (no web-server or frontend change needed: they pass `input` through).

## Constraints

Tool inputs can hold secrets: the displayed input must go through `factory_common.secrets.redact`. No schema change. Existing logs stay as they are.

## Open questions

None.
