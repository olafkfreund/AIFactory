---
status: approved
issue: 2067
author: olafkfreund
---

# Intent: CLI login terminal ownership (review fix, #2067)

## Problem
`start_cli_login_terminal` (apps/web-server/server/routes/cli_accounts.py) creates its PTY session without `owner_id`, so after #2067 owner binding `may_use()` denies every non-service user (GET/resize/close 404, /ws sees no session). It also stores the returned `PTYSession` as `terminal_id` and calls the non-existent `manager.write`, so the login command is never sent and `terminalId` is not a string (older bug).

## Proposed outcome
The signed-in user who starts the login terminal gets a string id, the login command runs, and only that user (or the service principal) can use the terminal.

## Affected users and systems
Org members using CLIAccountCard / LLMAccountsSettings; server/pty/access.py, routes/cli_accounts.py.

## Constraints
Fail closed for others. No weakening of project-bound sessions. A home-dir shell has no project, so it is bound to its owner alone.

## Open questions
None. (Larger question, out of scope: whether any org member should get a server-home shell at all.)
