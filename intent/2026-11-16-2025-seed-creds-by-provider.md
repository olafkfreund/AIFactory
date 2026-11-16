---
status: approved
issue: 2025
author: olafkfreund
---

# Intent: seed only the CLI logins a build Job uses

## Problem

`build_backend._inject_seed_creds` copies every file in `AIFACTORY_CLI_CREDS_SECRET` (Claude, Codex, Gemini, Copilot logins) into each build Job, whatever provider the run uses. An agent in the Job can read all of them. The Copilot files are a GitHub credential. The spec Job (#1932) already drops Copilot unless its model is `copilot:`; build Jobs do not.

## Proposed outcome

A build Job gets only the logins for the providers its phase models resolve to. Build and spec Jobs share one rule.

## Affected users and systems

Build Jobs and spec Jobs on the kubejob backend (`build_backend.py`, `spec_job_backend.py`).

## Constraints

Default-off behaviour (no secret configured) unchanged. Keep the diff small: other teams edit nearby (#2024, #2037). Fail toward fewer creds, never more.

## Open questions

None.
