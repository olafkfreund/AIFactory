---
status: draft
issue: Factory#813
author: olafkfreund
---

# Intent: clear the four real AIFactory CodeQL hygiene alerts

## Problem

CodeQL reports four real alerts on `dev`:

- 2431/2432 `py/log-injection`: `_refresh_to_remote_base` in
  `apps/web-server/server/services/build_backend.py` logs `base_branch`,
  `wt_path` and git's stderr raw. A branch name or stderr carrying CR/LF can
  forge log records. The same file already wraps these values in
  `sanitize_log()` elsewhere (line 1143).
- 2423 `py/redundant-assignment`: `terminal_status = terminal_status` in
  `apps/web-server/server/services/completion_orchestration.py:166` is a no-op.
- 1621 `py/uninitialized-local-variable`: in `apps/backend/core/enforcement.py`
  `_capture`, `msg_type` is first set inside the `try:`. If that line raises,
  the `except` handler's `logger.debug(..., msg_type)` raises
  `UnboundLocalError`, which breaks the "capture must never crash the stream"
  guarantee.

## Proposed outcome

The four alerts close on the next CodeQL run of `dev`. Behaviour is otherwise
unchanged: same log messages (escaped), same completion status, and a
`_capture` failure is always swallowed.

## Affected users and systems

AIFactory web-server build dispatch (log output only), completion
orchestration (no behaviour change), and the Claude enforcement wrapper in the
backend. No hosts, no config, no API.

## Constraints

- Smallest correct change; reuse `factory_common.logsafe.sanitize_log`.
- No change to log wording or levels.
- One PR into `dev`.

## Open questions

None.
