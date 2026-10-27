---
status: draft
issue: 1944
author: olafkfreund
---

# Intent: the Bash hook must not take its allowlist from a file the agent can write

## Problem

The worktree security profile `.aifactory/.aifactory-security.json` decides which
commands the Bash hook allows. The build agent can write that file: the file tools
are allowed on `./**` and only `.git` paths are guarded, and the Bash hook allows
ordinary shell writes into it. The hook (`security/profile.py`) re-reads the file
whenever its mtime changes and trusts every entry. Nothing re-applies
`DENY_COMMANDS` or `PLAN_GRANTABLE_COMMANDS`, so a hand-edited profile is honoured
even for denied network tools (verified on origin/dev). This affects every backend,
local and kubejob, and makes the #1940 fix incomplete on its own.

## Proposed outcome

A command the agent adds to the profile file, by any route, is not allowed. Commands
from detection, plan grants and the operator env override are still allowed.
File-tool writes to the profile are refused, and so are Bash commands that name it.

## Affected users and systems

AIFactory backend: `security/profile.py`, `security/hooks.py`,
`security/filesystem_validators.py`, `tools/executor.py` (non-Claude providers),
`project/analyzer.py` (plan seeding). All builds.

## Constraints

- Keep detection, plan grants (#1940) and `AIFACTORY_EXTRA_ALLOWED_COMMANDS` working.
- Do not change which commands detection grants.
- The file may still be written, for observability and other readers. It is just
  no longer an input to enforcement.

## Open questions

None. The lead chose option (a), an authoritative in-memory profile, over (b),
re-validating the file on load.
