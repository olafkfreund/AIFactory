---
status: draft
issue: 1948
author: olafkfreund
---

# Intent: script runners only from files tracked at the base ref

## Problem

Detection in `apps/backend/project/structure_analyzer.py` reads `Makefile`,
`package.json`, `pyproject.toml` and root `*.sh` from the worktree. The build
agent writes the worktree. The Bash hook checks only the runner name (`make`,
`npm`), never the target. So a target the agent adds is runnable.

Re-verified on origin/dev 8497484b, in a repo whose base commit has a
`Makefile` with `test` and a `package.json` with script `test`. The agent then
adds `evil` to both and writes `evil.sh`:

| command | today |
| --- | --- |
| `make test` | allowed |
| `make evil` | **allowed** |
| `npm run evil` | **allowed** |
| `./evil.sh` | refused (the parser passes the basename, see the #1968 spec) |
| `bash evil.sh` | allowed (`bash` is a base command, out of scope) |

`yarn evil` and `bun run evil` are already refused, because the parser emits
the script name as a command. `yarn run evil` and `pnpm run evil` are allowed.

## Proposed outcome

- Detection reads these files from the commit the worktree was cut from. That
  sha is recorded when the build starts, outside the worktree.
- `make <target>` and `npm|yarn|pnpm run <script>` (plus `npm test`/`start`/
  `stop`/`restart`) are allowed only for targets and scripts present at that
  commit.
- A target or script tracked at the base ref still runs.
- The hook docstring and `SECURITY.md` say the command allowlist is a guardrail,
  not containment (#1968 spec step 3).

## Affected users and systems

AIFactory build agents (kubejob and subprocess backends) and spec-creation
agents, via the Bash security hook. No gitops or cluster change.

## Constraints

- Owner decision on #1932 (2026-10-31, option B): agents run only in Jobs.
  #1948 is gate hardening: "runners only from files tracked at the base ref".
- Must not refuse a target that is tracked at the base ref.
- Must not lock out any human or service login, and must not deploy.
- #1968 spec step 2 (bwrap around the kubejob Job) is out: the owner chose no
  custom seccomp profile, and bwrap cannot create a namespace under
  RuntimeDefault, so it would fail open.

## Open questions

None. Known residuals: edits to a tracked target's recipe or script body,
npm `pre`/`post` lifecycle scripts, and `bash x.sh` or interpreters. The Job
pod boundary (option B) contains these, not the allowlist.
