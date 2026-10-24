---
status: approved
issue: Factory#813
intent: intent/2026-10-24-813-aif-codeql-hygiene.md
---

# Spec: clear the four real AIFactory CodeQL hygiene alerts

## Design

1. **2431/2432**: in `_refresh_to_remote_base`
   (`apps/web-server/server/services/build_backend.py:1016-1050`), wrap every
   logged argument in `sanitize_log()`, already imported at line 105:
   `base_branch`, `wt_path` and `fetch.stderr.strip() or "no stderr"` in the
   warning, `wt_path` and `base_branch` in the info. This is the same fix as the
   earlier alerts 2389-2391 and the call at line 1143.
2. **2423**: delete the line `terminal_status = terminal_status`
   (`completion_orchestration.py:166`). `terminal_status` is the function
   parameter (line 91), and it may be reassigned at line 135, so the value that
   reaches `status=terminal_status` is unchanged.
3. **1621**: in `_WrappedResponseStream._capture` (`core/enforcement.py:609`),
   add `msg_type: Any = None` before the `try:` and leave the existing
   assignment inside it. `getattr(msg, "type", None)` can raise when `type` is a
   raising property, so moving the line out of the `try` would let it crash the
   stream. Initialising it first keeps that line guarded and makes the handler
   always safe.

## Alternatives rejected

- Moving the `msg_type` assignment above `try:` (one of the triage options): a
  raising `type` attribute would then escape `_capture` and break the stream.
- Dismissing the alerts in the CodeQL UI: all four are real.
- A new logging helper: `sanitize_log` already does the job.

## Risks

- Log text changes only where a value carries control characters, which is
  the intended effect. Log wording stays the same.
- None for 2423, which is a pure no-op deletion.
- 1621: none. The only path it changes was a crash.

## Verification

- New test in `tests/claude_enforcement/test_wrapper_streaming.py`: a message
  whose `type` property raises goes through `_capture` without raising. It
  fails on `dev` today (UnboundLocalError) and passes after the change.
- Existing tests pass: `tests/claude_enforcement/`,
  `tests/test_build_backend_kubejob.py` (the #960 refresh tests at about lines
  1270 and 1329), and the completion orchestration tests.
- `ruff check` is clean on the three files.
- After merge, CodeQL on `dev` closes 2431, 2432, 2423 and 1621.
