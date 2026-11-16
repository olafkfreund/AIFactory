---
status: draft
issue: 2055
author: olafkfreund
---

# Intent: Low review findings, AIFactory (#2055, #2053)

## Problem
1. #2055: `_path_command_refusal` (apps/backend/security/hooks.py) refuses every command word with a `/` unless it is an absolute system binary, so `./gradlew`, `node_modules/.bin/x`, `.venv/bin/pytest` and `scripts/test.sh` are blocked for all agent runs. Its purpose (#2028) was only to stop an agent-written file such as `./echo` standing in for an allowed name.
2. #2053: the router dependency `project_github_token` reads `projectId` from path or query only. A body-only route naming the project in JSON (`/persist-token`) is neither access-checked nor bound.

## Proposed outcome
Relative project paths run when their basename passes the allowlist and is not a vetted base or validated command. A body `projectId` is authorised and bound like a path one. Any request without a resolvable project still gets no token (never a broader one).

## Affected users and systems
Agent runs (security hook); web-server GitHub routes in multi-installation mode.

## Constraints
No weakening of `./echo`, `sub/echo`, `/tmp/echo`, `..` refusals. Fail closed on tokens. A body `projectPath` is not trusted to pick an org.

## Open questions
None. Owner decision (not taken here): also allow `./<script>` for base-tracked scripts (spec #2028 item 3).
