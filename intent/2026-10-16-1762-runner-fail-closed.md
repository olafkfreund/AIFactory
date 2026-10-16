---
status: draft
issue: 1762
author: olafkfreund
---

# Intent: Runners fail closed when they cannot hide their environ

Part of #1762. The triage on dev narrowed the issue; this task covers parts (a)
and (c) only.

## Problem

(a) The agent runners call `process_hardening.make_non_dumpable()` before they
spawn an agent (`apps/backend/core/client.py:619`,
`apps/backend/core/simple_client.py:78`, `apps/backend/providers/factory.py:322`).
When the `prctl(PR_SET_DUMPABLE, 0)` call fails they only log a warning and go
on, so the agent's commands can read the runner's `/proc/<pid>/environ`, which
holds the GitHub token and the OAuth token. The web server does the opposite:
`_make_non_dumpable()` in `apps/web-server/server/main.py:781-797` raises
`SystemExit` and the server does not start.

(c) `child_env()` drops `_STRIP_VARS` with an exact-case match
(`apps/backend/core/child_env.py:57`), so `anthropic_api_key_file` in lowercase
reaches a child. Nothing reads a lowercase name today, but the strip list should
not depend on that.

## Proposed outcome

- A runner whose prctl call fails stops before it spawns an agent, with the same
  `SystemExit("prctl(PR_SET_DUMPABLE, 0) failed")` the server uses.
- On non-Linux machines nothing changes: `make_non_dumpable()` already returns
  True there, which is the only escape the server has. No new opt-out.
- `child_env()` strips a `_STRIP_VARS` name whatever its case.

## Affected users and systems

- Local and k8s runners (`run.py`, the CLI, the Codex/Gemini/Ollama providers).
- The web server, only if it shares the helper; its behaviour stays the same.

## Constraints

- Mirror the server exactly: same exit, same non-Linux behaviour, no env switch.
- Do not change the hardening itself or the order of the calls.

## Out of scope

(b) Credential files under HOME (`~/.git-credentials`, gh `hosts.yml`,
`~/.claude/.credentials.json`) need a design decision from the user and overlap
#1732 and #1724. The issue stays open for that part; the PR says "Part of #1762".

## Open questions

None.
