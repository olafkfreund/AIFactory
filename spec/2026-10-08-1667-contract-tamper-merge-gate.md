---
status: draft
issue: 1667
intent: intent/2026-10-08-1667-contract-tamper-merge-gate.md
---

# Spec: the merge gate acts only on the contract PFactory signed

This spec assumes the #1668 hotfix (3.8.2) has landed: `TRUSTED_PLAN_KEY` is in
`_AGENT_ENV_DENY_PATTERN` and `AIFACTORY_TOKEN` is on the exact deny list
(`apps/backend/core/auth.py:117-154`). The coding agent therefore cannot read
the HMAC key and cannot re-sign anything. This spec does not touch the scrub.

## Design

### 1. A copy of the signed contract that the web server holds

At ingest, the `/from-plan` route (`apps/web-server/server/routes/execution.py:1433`)
already holds the verified plan in memory. Right after `ingest_trusted_plan`
returns ok, the route also writes the plan, verbatim and with its `approval`
envelope, to the web server's data directory:

```
get_data_dir() / "trusted_contracts" / sha256(str(spec_dir.resolve())) + ".json"
```

- `get_data_dir()` is `~/.aifactory` (`apps/web-server/server/paths.py:30`).
  This is the user's home directory, not the project's `.aifactory`. It is
  outside every path the agent is granted: `./**`, `{project_path}/**`,
  `{spec_path}/**` and the original project's `.aifactory/` and `.worktrees/`
  (`apps/backend/core/client.py:740-760`). It is also outside the worktree that
  the #364 `rm`/`chmod` guards and the opt-in bwrap sandbox confine the agent
  to.
- The file is written with the existing `write_secret_file` (`paths.py:41`):
  0600 and atomic.
- **The record is not trusted because of where it is stored.** It is the
  signed plan, so its own HMAC protects it. A record that has been edited or
  replaced fails verification and holds the merge (section 3). Where it is
  stored only makes it hard to *delete*.
- The record is keyed on the resolved spec directory. That is the one value
  `merge_disposition`, `apply_path_risk_floor` and both handoff call sites
  already receive, so no signature has to grow a `project_path`.
- `ingest_trusted_plan` (`apps/backend/trusted_plan.py:803`) does not change.
  The backend package has no access to the web server's data directory, and
  the route is the only production caller (`grep ingest_trusted_plan(` finds one
  hit, `execution.py:1433`).

### 2. Deciding whether a task was ingested as trusted

A new web-server module, `server/services/trusted_contract.py`, exposes one
function:

```
resolve_contract(spec_dir) -> (state, contract)
    state ∈ {"legacy", "verified", "hold"}
```

It decides in this order:

1. **The web server holds a record for this spec directory.** The task is
   trusted, and the result is `verified` only if all three checks pass:
   - the record parses and passes `verify_plan_signature(record)`
     (`trusted_plan.py:257`). Called with no arguments, this loads the live
     keyring and retired kids from the environment. See section 4.
   - `context/task_contract.json` in the spec directory exists and parses.
   - it is canonically equal to the record (`_canonical`, `trusted_plan.py:162`).
     The only writer of that file is the ingest (`trusted_plan.py:845`, the
     single production write); the handoff changes an in-memory copy. Any
     difference means someone other than AIFactory wrote the file.

   If any check fails, the result is `hold`. When the result is `verified`,
   the contract returned is the **record**, never the spec-directory file.
2. **The web server holds no record, but the spec directory shows a trusted
   ingest.** This covers in-flight tasks (answer 5) and a record lost when the
   pod restarted (see Risks). A trace is any of the following:
   - an `approval` dict in `context/task_contract.json`;
   - an `approval` dict in `implementation_plan.json`;
   - `provenance.trusted_plan is True` in `requirements.json`, written by
     `_record_approval_provenance` (`trusted_plan.py:931-953`).

   The task counts as trusted. The result is `verified` only if
   `context/task_contract.json` exists, parses and passes
   `verify_plan_signature`. Otherwise it is `hold`. Once #1668 has landed, the
   agent cannot produce a valid signature, so a deleted, stripped or edited
   contract holds.
3. **No record and no trace.** The task is `legacy`: create-and-run, label-driven
   or a hand-made contract. It returns `load_task_contract(spec_dir)` and decides
   exactly as on `dev` today, including the #1658 `_contract_unreadable` hold
   (`pr_endgame.py:442-456`).

Any exception inside `resolve_contract` produces `hold`, except in the
`legacy` branch, which keeps today's behaviour.

### 3. The merge path consumes it, failing closed

- **`merge_disposition`** (`pr_endgame.py:459-517`). The
  `_contract_unreadable` and `load_task_contract` pair at `:497-499` is replaced
  by `resolve_contract`.
  - `hold` returns `HOLD_BLOCKING_DISPOSITION` (`pr_endgame.py:364`), with a
    constant log line ("task contract not verifiable; auto-merge withheld"). No
    path, state detail or file content goes into the log (py/log-injection, as
    at `:1302-1305`).
  - `legacy` keeps the `_contract_unreadable` check, unchanged.
  - The import-failure hold at `:485-494` stays. It now also covers the import
    of `trusted_contract`.
- **`apply_path_risk_floor`** (`pr_endgame.py:252-260`) reads the deployment
  block from `resolve_contract`. On `hold`, `floor = "blocking"`, raising the
  tier the same way `_path_risk_floor_closed` (`:287`) does. On `legacy` it keeps
  today's best-effort read. This can only tighten.
- **`satisfied_system_gates`** (`pr_endgame.py:318-341`) gets a keyword
  `trusted: bool`. When it is true, `task_metadata.json`'s
  `satisfiedSystemGates`/`satisfied_system_gates` are **not read**: the only
  gates counted are the verified contract's own `deployment.satisfied_gates`.
  - Nothing in the codebase writes those `task_metadata.json` keys
    (`grep satisfiedSystemGates` finds only readers).
  - A trusted task held on `system_gates` is cleared by a human merging it by
    hand. That is the same route every other `hold-blocking` takes.
  - Legacy tasks read both sources as before.

  Whether the signed `deployment.satisfied_gates` should count at all is #1663's
  question. This spec leaves it as it is.

### 4. Kid rotation (answer 1)

There is no new code. `verify_plan_signature`, called with its default
arguments, reads the current `AIFACTORY_TRUSTED_PLAN_KEY_*` keyring and
`AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS` at merge time (`trusted_plan.py:111-149`).
A kid that was retired or removed between ingest and merge fails, and the merge
holds. A re-sign (a new `/from-plan`) or a human merge clears it.

### 5. The TFactory handoff sends the verified contract (answer 4)

`build_ingest_payload(spec_dir, spec_id)` (`apps/backend/pfactory/tfactory_client.py:575`)
gets an optional keyword `contract: dict | None = None`.

- When the caller passes it, `build_ingest_payload` uses it instead of calling
  `load_task_contract` (`:621`).
- The additions after `:621` (`execution.phase_models` and
  `provenance.github_issue`) still apply on top. They change a copy, never the
  record.

Both production callers are in the web server process:
`routes/execution.py:927` (on-demand) and `maybe_auto_handoff_tfactory`
(`tfactory_client.py:795`, called from
`services/completion_orchestration.py:237` and `:283`).
`maybe_auto_handoff_tfactory` gets the same optional `contract` keyword and
passes it through. The web-server call sites call `resolve_contract` first and
pass:

- `verified` → the record;
- `hold` → `{}`, so TFactory infers. The handoff never blocks, and the merge
  hold already covers the task. One constant warning is logged.
- `legacy` → `None`, so the default path runs as today.

`load_task_contract` itself stays unchanged. It keeps skipping unparseable files
(`pr_endgame.py:446-448`), and the prompt builders
(`prompts_pkg/prompts.py:376/521/603`) and `migration_mapper.py:37` keep using
it or their own reads. Those are not gates.

### Out of scope (per the approved answers)

- The other `task_metadata.json` signals: `reviewTier`, `achievedVal`/`valFloor`,
  `ciParity` and the verdict. These are a separate issue (answer 3).
- `deploy_scaffold.py:84`, which is a follow-up (answer 4).
- The env scrub, which is #1668 (answer 2).

## Alternatives rejected

- **Move the contract out of the spec directory.**
  - It breaks the readers that legitimately use it in the agent's tree: the
    prompt builders (`prompts.py:376/521/603`), `migration_mapper.py:37`,
    `deploy_scaffold.py:84`, and TFactory's own expectation of
    `context/task_contract.json`.
  - The kubejob build has no access to the web server's home directory.
  - It still needs a record of "this task was trusted" to tell a deleted
    contract from a legacy task, which is the record this design adds anyway.

  Keeping the spec-directory copy and adding a server-held copy alongside it
  changes no reader.
- **Only re-verify the HMAC on the spec-directory file at merge.** This catches
  edits and stripping but not deletion. Deleting the contract (and the
  provenance) makes the task look legacy, and nothing holds. The server-held
  record is what closes deletion.
- **A database row (`Task` or a new table in `server/database/models.py`).** The
  row would survive pod restarts in-cluster, because Postgres comes from
  `DATABASE_URL`. But it needs an Alembic migration, and an async session
  inside the synchronous `merge_disposition`/`apply_path_risk_floor`. The `tasks`
  table is not populated for spec-dir tasks today (`Task(` has one constructor,
  `routes/task_service.py:1022`). The cost is larger than the gap it closes,
  because the trace rule (section 2.2) already fails closed when the record is
  lost. Decision D1 below.
- **Sign a new "trusted marker" with a second key.** That adds a new secret and
  rotation story for the same guarantee the existing PFactory signature gives
  over the verbatim record.
- **Store only a hash of the contract in the record.** It is the same size of
  change, but the merge path and handoff would then have to trust the
  spec-directory file for the content, and a deleted file could not be
  recovered for the handoff. The full plan is a few KB.
- **Hold only when the deployment block differs, not on any difference.** That
  needs a field-by-field diff. Any difference to a file only the ingest writes
  is evidence of tampering, and the approved intent says tampered contracts
  hold.

## Risks

- **The record is lost on a pod restart in-cluster.** `aifactory-data`, mounted
  at `/home/nonroot/.aifactory`, is an `emptyDir`
  (`charts/aifactory/templates/deployment.yaml:684-685`, `:773-774`). After a
  restart, trusted tasks fall to the trace rule (2.2). That still fails closed
  on an edited, stripped or deleted contract while any trace remains. The gap
  that remains is an agent that deletes the contract **and** the provenance
  **and** the `approval` in `implementation_plan.json`, on a pod that has
  restarted since ingest. That task reads as legacy and decides as today.
  Closing it needs a durable store (D1).
- **The spec directory at merge time differs from the one at ingest.** The
  ingest uses `project/.aifactory/specs/<id>` (`execution.py:1391-1394`). If the
  completion or endgame path passes the worktree spec directory
  (`review_redrive_service.worktree_spec_dir`, `.../worktrees/tasks/<id>/...`),
  the record is missed and the trace rule applies. That fails closed only while
  traces remain. The plan must confirm the `spec_dir` given to
  `run_pr_endgame` and the handoff (`completion_orchestration.py:237/283/501`)
  is the main spec directory, and resolve to it if not.
- **A false hold on a trusted task whose contract was rewritten in good
  faith.** No code rewrites `context/task_contract.json` after ingest (single
  writer, `trusted_plan.py:845`). Operators who hand-edit it get a hold, which
  is the intended behaviour.
- **Key rotation done badly holds in-flight merges.** Removing a key before its
  tasks merge holds them. This is answer 1's chosen behaviour. The rotation doc
  (`docs/docs/compliance/trusted-plan-key-rotation.md`) gets one sentence
  saying so.
- **A local or dev host with the bwrap sandbox off.** The agent's Bash can still
  reach `~/.aifactory` through a shell redirect, because #364 guards only
  `rm`/`chmod`. It cannot forge a record (no key after #1668), so editing or
  replacing a record holds. Deleting the record falls to the trace rule.
- **TFactory gets `{}` on a held task and infers.** That verify run tests less
  than the declared ACs. The merge is held regardless.

## Verification

Write the tests first. Each one marked **(fails on dev)** must fail against
`origin/dev` before any implementation edit.

New `apps/web-server/tests/test_trusted_contract_merge.py`. It uses a real
`sign_plan` with a test keyring set through `monkeypatch.setenv`, a `tmp_path`
spec directory, and `get_data_dir` patched to `tmp_path`.

1. A trusted ingest whose contract declares `production_classification:
   production`, then `deployment` is stripped from
   `context/task_contract.json` → `merge_disposition` returns `hold-blocking`.
   **(fails on dev)**: dev decides on the stripped contract.
2. Trusted ingest, then `context/task_contract.json` is deleted → `hold-blocking`.
   **(fails on dev)**: dev treats an absent file as legacy.
3. Trusted ingest, then the spec-directory contract is edited and **re-signed
   with the real test key**, simulating the pre-#1668 leak → `hold-blocking`,
   because it no longer equals the record. **(fails on dev)**
4. Trusted ingest with `system_gates: [human-approval]`, then
   `satisfiedSystemGates: ["human-approval"]` is added to `task_metadata.json` →
   still `hold-blocking`. **(fails on dev)**
5. Trusted ingest, then the kid is added to `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS`
   before merge → `hold-blocking`. **(fails on dev)**
6. Trusted ingest, then one byte of the record file is edited → `hold-blocking`.
   **(fails on dev)**: there is no record on dev, so write the test against the
   spec-directory contract and the expected record path.
7. An in-flight task: no record, `requirements.json` has
   `provenance.trusted_plan: true`, and the contract is deleted →
   `hold-blocking`. **(fails on dev)**
8. An in-flight task: no record, and a valid signed contract that has not been
   touched → decides exactly like the same task on dev (for example
   `auto-merge` for a low-tier, non-production contract). This guards against
   holding everything.
9. A legacy task: no record, no trace, no contract → same disposition as dev.
   A legacy task with an unparseable `context/task_contract.json` still holds
   under #1658. These are regression guards and pass on dev.
10. `apply_path_risk_floor` on a trusted task with a deleted contract → floor
    `blocking`. **(fails on dev)**
11. `build_ingest_payload(spec_dir, id, contract=record)` with an edited
    spec-directory contract → `payload["contract"]` carries the record's
    `deployment` and `tfactory` blocks, plus the added `phase_models`. The
    handoff caller on `hold` passes `{}` and does not raise. **(fails on
    dev)**: the `contract` keyword does not exist there.
12. `/from-plan` (the route test, existing fixtures) writes the record at the
    expected path with mode 0600. **(fails on dev)**

Existing suites must stay green, with no assertion loosened:
`apps/web-server/tests/test_pr_endgame_merge_gate.py` (#1658),
`apps/backend/pfactory/test_tfactory_client_issue.py`,
`apps/backend/pfactory/test_tfactory_source_branch.py`, and the trusted-plan
tests.

Commands:

```
cd apps/web-server && ../backend/.venv/bin/pytest tests/test_trusted_contract_merge.py tests/test_pr_endgame_merge_gate.py -q
cd apps/backend && .venv/bin/pytest pfactory/ -q -k "tfactory_client"
```

Runtime check on the dev cluster:

1. Run a `/from-plan` with a production-classified plan.
2. Strip `deployment` from the spec contract by hand.
3. Confirm the endgame logs "task contract not verifiable" and the PR is not
   auto-merged.

## Decisions for the approver

- **D1. Durable record store.** Should the record use the data-directory file
  (smallest, but lost on a pod restart in-cluster; the trace rule then applies)
  or a database table (durable, but needs a migration and an async session)?
  *Recommended:* the file now, and a follow-up issue to put `aifactory-data` on
  a PVC, or move the record to the database, if the residual gap in Risks
  matters in-cluster.
- **D2. Trusted tasks ignore `satisfiedSystemGates` in `task_metadata.json`
  entirely.** A human merge becomes the only way to clear a `system_gates` hold.
  *Recommended:* yes. Nothing in the code writes that key today.
- **D3. A held task's TFactory handoff sends `{}` (TFactory infers) rather than
  skipping the handoff.** *Recommended:* send `{}`, which keeps answer 4's
  "does not block".
