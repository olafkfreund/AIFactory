---
status: approved
issue: 2101
author: olafkfreund
---

# Intent: marker-nonce dispatch test must not read the shared settings DB

## Problem

`tests/test_marker_nonce.py::test_kubejob_dispatch_puts_the_nonce_in_the_job_env`
fails with `project settings unavailable: stored settings cannot be decrypted`.
Reproduced on origin/dev, with that test alone (no ordering involved).
`KubeJobBuildBackend.dispatch` calls `project_settings_store.mcp_spawn_env`
(build_backend.py:1455), which opens the process-wide default engine
(`server.database.engine`: `DATABASE_URL`, else `~/.aifactory/data.db`) and reads the row for
`/home/nonroot/.aifactory/workspaces/p`. Any row for that path sealed under a
different KMS key (a developer's real DB, or a row another test wrote into the
shared default DB) makes `secret_field.unseal` return "" and dispatch raise.
This is the designed fail-closed behaviour (project_settings_store.py:187), not a
bug. The test is simply not hermetic: its siblings stub settings
(`test_build_backend_kubejob.py` autouse `_empty_project_settings`;
`test_marker_nonce.py` stubs `mcp_spawn_env` in the spawn tests); this one does not.

## Proposed outcome

The test passes regardless of what is in the default DB or KMS key, and still
proves the nonce and task id reach the Job env.

## Affected users and systems

Root test suite only (`pytest tests/ -m "not slow"`). No production code change.

## Constraints

No skip or xfail. Do not weaken the fail-closed decrypt behaviour.

## Open questions

None. Decision (owner delegation 2026-11-17): fix the test (isolate settings),
not the code, because the code behaves as designed and the sibling tests already
use this isolation.
