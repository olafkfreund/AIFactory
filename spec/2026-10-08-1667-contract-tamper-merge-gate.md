---
status: draft
issue: 1667
intent: intent/2026-10-08-1667-contract-tamper-merge-gate.md
---

# Spec: the merge gate acts only on the contract PFactory signed

This spec assumes that the #1668 hotfix (3.8.2) and #1676 have landed.
`TRUSTED_PLAN_KEY` is in `_AGENT_ENV_DENY_PATTERN`, `AIFACTORY_TOKEN` is on the
exact deny list (`apps/backend/core/auth.py:117-154`), and the simple database
clients are scrubbed. This spec does not touch the scrub.

## New finding: the scrub does not cover an in-process build

The scrub blanks secrets in the Claude CLI's environment only (`options.env`,
`core/client.py:611`). The `run.py` process above the CLI is spawned with the
full web-server environment, minus the Anthropic key only (`make_subprocess_env`,
`apps/web-server/server/utils/subprocess_env.py:30-55`). On Linux, a process
can read `/proc/<pid>/environ` of another process owned by the same user, even
with Yama `ptrace_scope=1`. This was checked on the dev box. `cat` and `tr` are
on the agent's Bash allowlist (`apps/backend/project/command_registry/base.py`),
and nothing guards `/proc` (`grep /proc apps/backend/security` finds nothing).

An in-process agent can therefore read `AIFACTORY_TRUSTED_PLAN_KEY_*` and
`DATABASE_URL` from its parent's environment.

The bwrap `fs` sandbox does not change this unless it has its own PID namespace.
Without `AIFACTORY_AGENT_SANDBOX_PIDNS` it read-only binds the host's `/proc`
(`apps/web-server/server/services/sandbox.py:146-149`).

A kubejob build is not exposed:

- it runs in its own pod;
- its environment is an allowlist (`build_job_env`,
  `services/build_backend.py:329-350`), with no `DATABASE_URL` and no
  trusted-plan key (`build_backend.py:63`, `:231`).

So "isolated" in this spec means one of these two:

- a kubejob build;
- an in-process build under a working `fs`/`strict` bwrap sandbox **with** a PID
  namespace.

No server-side store, file or database is safe from an in-process build that is
not isolated. This narrows the user's (c), and is decision D4 below.

## Design

### 1. The trusted record lives in the web-server database (a)

There is a new table, `trusted_contracts`. Its model is in
`apps/web-server/server/database/models.py`, next to `JobState` (`:1123`).

| column | type | note |
| --- | --- | --- |
| `spec_key` | `String(64)` PK | sha256 of `str(spec_dir_for(project_path, spec_id).resolve())` |
| `spec_id` | `String(255)` | for operators |
| `contract` | `Text` | the signed plan, verbatim, with its `approval` envelope |
| `build_isolation` | `String(16)` nullable | `kubejob` / `sandbox-pidns` / `none`. Stamped at spawn, see section 3 |
| `created_at` / `updated_at` | `DateTime` | as on `JobState` |

- **Migration.** A new Alembic revision,
  `server/database/alembic/versions/20261008_<rev>_trusted_contracts.py`, with
  `down_revision = "c1f5a3d7b924"`, the current head
  (`20260829_c1f5a3d7b924_audit_resource_id_width.py`). It is shaped like
  `20260620_b7e1c9a4d2f3_job_states.py`.
- **Startup.** `init_db` (`server/main.py:125`) already upgrades to head and
  verifies the schema (`database/engine.py:76-83`). No new startup code.
- **Which database.** It is the engine's database: Postgres in-cluster
  (`DATABASE_URL`, `engine.py:36-46`) and SQLite at `~/.aifactory/data.db`
  locally.
- **Store.** A small async store class, `server/services/trusted_contract_store.py`,
  uses `async_session_factory` the same way `JobStateStore` does
  (`services/job_state_store.py:120-129`). It has three methods:
  - `put(spec_key, spec_id, contract)` overwrites, because a re-ingest of the
    same spec replaces the record;
  - `get(spec_key) -> TrustedRecord | None`, where `TrustedRecord` is a frozen
    dataclass holding `contract: dict` and `build_isolation: str | None`;
  - `stamp_isolation(spec_key, kind)`, described in section 3.
- **Write at ingest.** The `/from-plan` route (`routes/execution.py:1433`) is
  async. Right after `ingest_trusted_plan` returns ok, it calls
  `await store.put(...)`. If the write fails, the route returns 500 and does
  not start the build. A trusted build with no record must never run.
  `ingest_trusted_plan` (`apps/backend/trusted_plan.py:803`) does not change.
- **The record is still self-verifying.** Its HMAC is re-checked on every read
  (section 2), so an edited row holds even where the database could be reached.

### 2. `resolve_contract`: one pure, synchronous decision

**Crossing the sync/async boundary (the earlier objection).** The database is
read once, in async code, and passed down as a plain value. Nothing synchronous
touches a session.

`server/services/trusted_contract.py` exposes:

```
resolve_contract(spec_dir, record: TrustedRecord | None | LookupFailed)
    -> (state, contract)          state ∈ {"legacy", "verified", "hold"}
```

It is pure and synchronous: no I/O except reading the spec directory.

The async callers do `await store.get(...)` and pass the result through a new
**required** keyword argument, `trusted=`. Making it required means a missed
caller fails in tests rather than defaulting open. The callers are:

- `run_terminal_completion` (`services/completion_orchestration.py:79`, async).
  It looks the record up once, before the path floor at `:219`, and threads it
  to `apply_path_risk_floor`, `gather_pr_context` → `apply_path_risk_floor`
  (`pr_endgame.py:1218`), and `run_pr_endgame(trusted=...)`
  (`completion_orchestration.py:500`).
- `run_pr_endgame` (`pr_endgame.py:1232`, async) passes it to
  `merge_disposition(spec_dir, tier, trusted=...)` at `:1300`.
- The handoff callers (section 5).

A database exception becomes `LookupFailed`, which resolves to `hold`.

The decision runs in this order:

0. **The host is not isolated.** If `host_isolated()` is false, the result is
   `hold` for any task that is or might be trusted, as worded in D4.
   `host_isolated()` is a pure read of the **web server's own** environment at
   merge time, which the agent cannot change. It is true when either:
   - `build_backend.selected_backend() == "kubejob"` (`build_backend.py:358`), or
   - `sandbox.is_enabled()` (`sandbox.py:106`) is true and `sandbox._mode()` is
     `fs` or `strict` and `_pidns_enabled()` (`:91`) is true.
1. **A record exists.** The task is trusted. The result is `verified` only if
   all of the following hold; otherwise it is `hold`:
   - `verify_plan_signature(record.contract)` passes (`trusted_plan.py:257`,
     with the live keyring and retired kids);
   - `record.build_isolation` is `kubejob` or `sandbox-pidns` (rule (c), see
     section 3);
   - `context/task_contract.json` exists, parses, and is canonically equal to
     the record (`_canonical`, `trusted_plan.py:162`). Its only writer is the
     ingest (`:845`).

   When the result is `verified`, the contract returned is the record.
2. **No record, but the spec directory shows a trusted ingest.** This covers
   in-flight tasks ingested before this ships (answer 5). A trace is any of:
   - `approval` in `context/task_contract.json` or in `implementation_plan.json`;
   - `provenance.trusted_plan is True` in `requirements.json`
     (`trusted_plan.py:931-953`).

   The result is `hold` unless the spec contract verifies by HMAC. A task with
   no record also has no isolation stamp, so under (c) it holds. In practice
   every in-flight trusted task holds once, and a human merges or re-signs it.
   See D5.
3. **No record and no trace.** The task is `legacy`. It gets
   `load_task_contract(spec_dir)` and today's behaviour, including the #1658
   `_contract_unreadable` hold (`pr_endgame.py:442-456`).

### 3. Rule (c): how the merge path knows how the build ran

The web server stamps `build_isolation` onto the record **when it spawns the
build**, from its own state. The agent never supplies it. There are two spawn
points:

- **In-process**: `AgentService._spawn_task_execution`
  (`services/agent_service.py:927`). It calls
  `build_sandboxed_command(cmd, project_path)` at `:1220`, and that call falls
  back silently to the bare command when bwrap is missing or broken
  (`sandbox.py:126-128`). The stamp therefore comes from the result, not the
  setting:
  - `sandbox-pidns` if `cmd[0]` is the bwrap binary **and** `--unshare-pid` is
    in the arguments;
  - `none` otherwise.
- **Kubejob**: `KubeJobBuildBackend.dispatch` (`services/build_backend.py:1204`)
  stamps `kubejob` beside its existing `set_worker_ref`.

`stamp_isolation` is **sticky to the weakest value**. Once `none` is stamped,
a later spawn never overwrites it: a re-run, recovery or fixer re-spawn of a
build that was ever unisolated stays held. An empty stamp means "no spawn seen"
and holds.

The plan must list every other `run.py` spawn site, for example the queue
drain through `_start_build_unit` (`agent_service.py:903`) and the review
re-drive. Each one either routes through the two points above or stamps
itself. A missed site leaves the stamp empty, which holds, so the failure is
closed.

The stamp lives in the same database row. That row is out of reach of an
isolated build: a kubejob build has no `DATABASE_URL`, and a PID-namespaced
bwrap build cannot see the web server's `/proc` and does not have its home
directory mounted (`sandbox.py:150-153`). A build that is not isolated could
forge the stamp, but on a host that is not isolated step 0 already holds.
Checking both the stamp and the host covers a host whose configuration
changed between build and merge.

### 4. The merge path consumes it, failing closed

- **`merge_disposition`** (`pr_endgame.py:459-517`). `resolve_contract` replaces
  the `_contract_unreadable`/`load_task_contract` pair at `:497-499`.
  - `hold` returns `HOLD_BLOCKING_DISPOSITION` (`:364`), with a constant log
    line ("task contract not verifiable or build not isolated; auto-merge
    withheld"). No path or content goes into the log (py/log-injection, as at
    `:1302-1305`).
  - `legacy` keeps the `_contract_unreadable` check.
  - The import-failure hold (`:485-494`) also covers the new modules.
- **`apply_path_risk_floor`** (`:252-260`). On `hold`, `floor = "blocking"`, as
  `_path_risk_floor_closed` (`:287`) does. On `legacy`, it behaves as today.
- **`satisfied_system_gates`** (`:318-341`) takes a keyword `trusted: bool`. When
  it is true, `task_metadata.json`'s `satisfiedSystemGates` /
  `satisfied_system_gates` are not read (D2).
- **Kid rotation (answer 1).** There is no new code. `verify_plan_signature`
  reads `AIFACTORY_TRUSTED_PLAN_KEY_*` and `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS`
  at merge time (`trusted_plan.py:111-149`), so a retired or removed kid holds.

### 5. The TFactory handoff sends the verified contract (answer 4, D3)

`build_ingest_payload` (`apps/backend/pfactory/tfactory_client.py:575`) and
`maybe_auto_handoff_tfactory` (`:795`) each get an optional keyword,
`contract: dict | None = None`.

When `contract` is given, it replaces `load_task_contract` at `:621`. The
`phase_models` and `provenance.github_issue` additions are still applied on a
copy.

The web-server callers resolve the contract first and pass:

- `verified` → the record;
- `hold` → `{}`, so TFactory infers. One constant warning is logged, and the
  handoff never blocks;
- `legacy` → `None`, which is today's path.

The callers are:

- `routes/execution.py:927` (async route);
- `services/completion_orchestration.py:237` and `:283` (async).

`load_task_contract` itself, the prompt builders and `migration_mapper` do not
change.

### 6. Which spec directory is the key

Ingest uses the main spec directory, `project/.aifactory/specs/<id>`
(`execution.py:1391-1394`). The record is keyed on
`spec_dir_for(project_path, spec_id).resolve()` (`server/specpath.py:54`), and
the spawn points compute the same key.

**The plan must confirm** that the `spec_dir` reaching `run_terminal_completion`,
`run_pr_endgame` and the handoffs (`completion_orchestration.py:219/237/283/501`)
resolves to the same path, and not to the worktree copy
(`review_redrive_service.worktree_spec_dir`). Where it does not, the caller
must derive the key from `(project_path, spec_id)` instead.

A key that does not match finds no record. That falls to the trace rule
(section 2, step 2), which holds while any trace remains. On an isolated host,
a build cannot delete traces from both the record and the spec directory.

### Out of scope

These follow the approved answers:

- other `task_metadata.json` signals, which are a separate issue;
- `deploy_scaffold.py:84`, which is a follow-up;
- the scrub (#1668/#1676).

The `/proc/<pid>/environ` exposure is real independently of this issue. It
should get its own issue, either to give `run.py` a scrubbed environment or to
make PID-namespace isolation the default (see Risks).

## Decisions

Approved (user, 2026-10-08):

- **(a)** The trusted record lives in the web-server database. In-cluster that
  is Postgres.
- **(c)** A trusted task is never auto-merged when its build was not isolated.
  The meaning of "isolated" is narrowed in D4, which is still open.
- **D2.** Trusted tasks ignore `satisfiedSystemGates` in `task_metadata.json`. A
  human merge clears a `system_gates` hold.
- **D3.** A held task hands off to TFactory with `{}`.

Open for the approver:

- **D4. "Isolated" means kubejob, or a bwrap `fs`/`strict` sandbox *with a PID
  namespace*. A plain `fs` sandbox is not enough.** The finding above shows
  that `fs` without a PID namespace still exposes the HMAC key and
  `DATABASE_URL` through `/proc`.

  The consequence is that on a host that is not isolated, the merge path cannot
  trust any of its own state, including the record or its absence. Step 0
  therefore holds every task that is or might be trusted. A task whose traces
  have all been deleted looks legacy, and on such a host that cannot be
  prevented. There are two options:
  - **D4-i:** on a host that is not isolated, hold **all** auto-merges,
    including legacy tasks. This closes the gap completely. It contradicts the
    intent's "legacy decides as today" on that configuration only, and an
    operator re-enables auto-merge by turning on kubejob or a PID-namespaced
    sandbox.
  - **D4-ii:** hold only tasks with a record or trace, and document that a
    build which is not isolated can disguise a trusted task as legacy.

  *Recommended:* D4-i, but only after checking the live cluster. The code
  default is `subprocess` (`build_backend.py:121`), and the chart does not set
  `AIFACTORY_BUILD_BACKEND`. #671 is described in code comments as having made
  kubejob live, but this has not been verified against the running deployment.
  - If the cluster runs kubejob, D4-i costs nothing there and affects only
    local and unprivileged in-pod hosts.
  - If the cluster runs in-process builds, D4-i stops all auto-merge until
    kubejob or a PID-namespaced sandbox is switched on.
- **D5. In-flight trusted tasks at rollout.** They have no record and no
  isolation stamp, so they all hold once.
  *Recommended:* accept this, and say so in the release note. A human merges
  them or re-runs `/from-plan`.

## Alternatives rejected

- **Move the contract out of the spec directory.** This breaks the readers that
  use it in the agent's tree: the prompt builders (`prompts.py:376/521/603`),
  `migration_mapper.py:37`, `deploy_scaffold.py:84`, TFactory, and kubejob
  builds, which have no access to the server's home directory. A "was trusted"
  record would still be needed to tell deleted from legacy.
- **A file in `~/.aifactory`** (the previous draft). An in-process agent can
  delete it with `mv`, `python3` or a shell redirect, because only `rm`/`chmod`
  are path-guarded (`apps/backend/security/filesystem_validators.py:103/139`;
  `security/parser.py:109` skips redirect targets). It is also lost on a pod
  restart (`aifactory-data` is an `emptyDir`,
  `charts/aifactory/templates/deployment.yaml:684`, `:773`).
- **Re-check the HMAC only.** This misses deletion, and an in-process agent can
  re-sign using the key it reads from `/proc`.
- **(c) with a plain `fs` sandbox.** This is the subject of D4.
- **A synchronous database session, or `asyncio.run` inside `merge_disposition`.**
  There is a running event loop at that point, because `run_pr_endgame` is
  async. A second, synchronous engine would duplicate the engine configuration.
  Passing a value that was already fetched is smaller and keeps the decision
  pure.
- **Sign a separate "trusted" marker with a new key.** It adds a secret and a
  rotation story, and gives no more than the PFactory signature already gives
  over the verbatim record.
- **Store only a hash of the contract.** The handoff would then have to trust
  the spec-directory file for the content.

## Risks

- **The in-process deletion gap needs no restart.** On the previous draft's
  design, an in-process agent could delete the server copy and the spec-directory
  traces during the build, and the task merged as legacy. With (a)+(c) plus D4:
  - on an isolated host, the record is out of the build's reach, and deletion
    or editing holds;
  - on a host that is not isolated, step 0 holds every task that might be
    trusted. What remains open is a fully disguised task (record unreachable or
    forged, all traces deleted), and it is closed only under D4-i.
- **What (a)+(c) leave open on an isolated host.**
  - A kubejob build shares the spec directory on the data PVC with the web
    server. Edits there are detected as a mismatch and hold.
  - A PID-namespaced bwrap build keeps network access in `fs` mode, but it has
    no database credentials to use.
  - Nothing known remains open.
- **The host's configuration changes between build and merge.** The sticky
  per-build stamp covers this.
- **The database is unavailable at merge.** `LookupFailed` holds. Under D4-i the
  same is true for legacy tasks. This is fail-closed, consistent with #1658.
- **The migration on upgrade.** The table is new, with no backfill.
  `_verify_schema_at_head_sync` refuses to start on a database that has not been
  migrated, which is the existing behaviour.
- **Local SQLite** (`~/.aifactory/data.db`) can be deleted by an in-process
  agent that is not isolated. That host is not isolated, so step 0 (and D4-i)
  is what covers local. A local host is protected only by D4-i, or by running
  with a PID-namespaced sandbox.
- **A missed spawn site** leaves the stamp empty, which holds. The plan
  enumerates the sites.
- **Kid rotation done carelessly holds in-flight merges** (answer 1). The
  rotation doc gets one sentence saying so.
- **The `/proc` exposure also undermines #1668 for in-process builds generally,**
  beyond this issue. It needs a separate issue.

## Verification

Write the tests first. Each test marked **(fails on dev)** must fail against
`origin/dev` before any implementation edit.

The new file is `apps/web-server/tests/test_trusted_contract_merge.py`. It
uses:

- a real `sign_plan` with a test keyring set through `monkeypatch.setenv`;
- an in-memory SQLite `async_session_factory` fixture, at Alembic head;
- `tmp_path` spec directories;
- `AIFACTORY_BUILD_BACKEND=kubejob` (an isolated host) unless the test says
  otherwise.

**Tamper (a record exists, isolation is `kubejob`):**

1. Strip `deployment` from the spec contract → `hold-blocking`.
   **(fails on dev)**
2. Delete the spec contract, the `requirements.json` provenance and the plan
   `approval` → `hold-blocking`. The record still exists. **(fails on dev)**
3. Edit the spec contract and re-sign it with the real test key →
   `hold-blocking`, because it no longer equals the record. **(fails on dev)**
4. Add `satisfiedSystemGates: ["human-approval"]` to `task_metadata.json` on a
   contract with `system_gates` → still held. **(fails on dev)**
5. Retire the kid before merge → `hold-blocking`. **(fails on dev)**
6. Change one byte of the record's `contract` → `hold-blocking`.
   **(fails on dev)**

**(a) Durable record:**

7. After the migration, `trusted_contracts` exists at head, and
   `_verify_schema_at_head_sync` passes. **(fails on dev)**
8. `/from-plan` writes the row; if the store raises, the route returns 500 and
   the build does not start. **(fails on dev)**
9. `store.get` raises → `merge_disposition` returns `hold-blocking`.
   **(fails on dev)**

**(c) Isolation:**

10. The record has `build_isolation="none"` → `hold-blocking`. **(fails on dev)**
11. The record has `build_isolation=None` (never stamped) → `hold-blocking`.
    **(fails on dev)**
12. `_spawn_task_execution` with bwrap absent stamps `none`; with bwrap and
    PID-namespace mode it stamps `sandbox-pidns`; `KubeJobBuildBackend.dispatch`
    stamps `kubejob`. All three use fakes. **(fails on dev)**
13. Stickiness: a `none` stamp followed by a `kubejob` stamp stays `none`.
    **(fails on dev)**
14. Host not isolated (`AIFACTORY_BUILD_BACKEND=subprocess`, sandbox off) with a
    record stamped `kubejob` → `hold-blocking`. **(fails on dev)**
15. Under D4-i: a host that is not isolated with a legacy task →
    `hold-blocking`. Under D4-ii it decides as dev. **(fails on dev under D4-i)**

**Regression guards (pass on dev):**

16. An isolated host, a record stamped `kubejob` and an untouched contract →
    the same disposition dev gives the same contract. This shows the change
    does not hold everything.
17. An isolated host and a legacy task (no record, no trace) → the same as dev,
    including the #1658 hold on an unparseable contract.
18. An in-flight task (no record, a valid trace) → `hold-blocking` per D5.
    **(fails on dev)**

**Path floor and handoff:**

19. `apply_path_risk_floor` with `trusted=` resolving to `hold` → `blocking`.
    **(fails on dev)**
20. `build_ingest_payload(..., contract=record)` with an edited spec contract →
    the payload carries the record's `deployment` and `tfactory` blocks plus the
    added `phase_models`. On `hold`, the callers pass `{}` and do not raise.
    **(fails on dev)**

These existing suites must stay green, with no assertion loosened:

- `tests/test_pr_endgame_merge_gate.py` (#1658);
- `apps/backend/pfactory/test_tfactory_client_*.py`;
- the job-state and migration tests;
- the trusted-plan tests.

Commands:

```
cd apps/web-server && ../backend/.venv/bin/pytest tests/test_trusted_contract_merge.py tests/test_pr_endgame_merge_gate.py -q
cd apps/backend && .venv/bin/pytest pfactory/ -q -k tfactory_client
```

Runtime check on the dev cluster (kubejob):

1. Run `/from-plan` with a plan classified as production.
2. Strip `deployment` from the spec contract on the PVC.
3. Confirm the endgame logs the constant hold line and does not auto-merge.
4. Confirm `trusted_contracts.build_isolation = 'kubejob'` for that spec.
