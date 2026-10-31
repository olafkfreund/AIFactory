---
status: approved
issue: 1948
author: olafkfreund
---

# Intent: indirect execution through agent-written scripts and runners

## Problem

The Bash hook validates each command against the project allowlist and the
per-command validators (git, process, filesystem, database). A command that
runs a file the agent wrote runs that file's contents with no per-command
check. `structure_analyzer.py` turns repo files into allowed runners
(`./<name>.sh`, `make`, `npm`/`yarn`/`pnpm`/`bun`, Poetry/PEP 621
scripts). Detection re-runs at every session seed since #1947, so a runner the
agent adds mid-build is picked up. #1948 widened this from `./*.sh` to every
detected runner.

Checked on origin/dev (2026-10-30) with `security.hooks.validate_command` in
a fresh repo with a `Makefile` and a `package.json`:

| Command | Allowed |
| --- | --- |
| `./evil.sh` | no: the parser passes `evil.sh`, so the `./` branch in `project/__init__.py:93` is not reached |
| `bash evil.sh`, `sh evil.sh` | **yes**: `bash`/`sh` are `BASE_COMMANDS` |
| `make test`, `npm run test`, `npm test` | yes |
| `curl http://x` | yes: egress policy defaults to `off` |

So the gap is wider than detection. Any base shell runs any script. Test
runners, build tools and package lifecycle hooks (pytest `conftest.py`,
`npm install` postinstall, `build.rs`, a recipe the agent edits in a
base-tracked `Makefile`) also run agent-written code, by design. The agent's
reflex of "the command was refused, so put it in a script" bypasses every
per-command validator.

## Proposed outcome

A decision, recorded in a spec, on what boundary contains indirect execution.
Operators and reviewers know what the command layer does and does not
guarantee. No reviewer has to rediscover this class from a script runner.

## Affected users and systems

- AIFactory backend: `security/hooks.py`, `project/structure_analyzer.py`,
  `project/analyzer.py` (re-detection), `core/client.py` (bwrap sandbox flag).
- Deployed factories: `AIFACTORY_BASH_SANDBOX=false` (the SDK bash sandbox) in
  the AIFactory, PFactory and TFactory manifests (k3d cannot host it, #363).
  The agent OS sandbox (`AIFACTORY_AGENT_SANDBOX`, `server/services/sandbox.py`)
  can run in-pod but is not set in factory-gitops, so it is off.
- #1932 (in-pod isolation from the control plane), #1944 and #1940 (profile and
  plan grants, closed).

## Constraints

- Must not break legitimate builds, which add Makefile targets, npm scripts and
  test files as part of the work, and run them.
- No cluster or gitops change in this task. No code this wave (docs only).

## Open questions

- Accept the per-command layer as a guardrail and put containment in the pod
  boundary (#1932)? This is the recommendation in the spec.
