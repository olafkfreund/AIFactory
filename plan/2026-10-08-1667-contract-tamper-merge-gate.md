---
status: approved
issue: 1667
spec: spec/2026-10-08-1667-contract-tamper-merge-gate.md
---

# Plan: the merge gate acts only on the contract PFactory signed

All line numbers below are from `origin/dev` at `d4e18ce0`, which includes
#1668, #1676 and #1678. This plan can be implemented without opening the intent
or the spec.

## The problem

The live merge decision, `merge_disposition` in
`apps/web-server/server/services/pr_endgame.py`, reads
`context/task_contract.json` through `load_task_contract`
(`apps/backend/pfactory/tfactory_client.py:87`), which is a plain JSON read.
The coding agent can edit, strip or delete that file because it has Write/Edit
on `{spec_path}/**`.

The agent can also reach the web server's secrets when the build runs
in-process (a subprocess of the web server, not a kubejob):

- `run.py` inherits the web server's full environment;
- the agent can `cat /proc/<parent pid>/environ`, which works for any process
  owned by the same user, even with Yama `ptrace_scope=1`;
- that environment holds `AIFACTORY_TRUSTED_PLAN_KEY_*` and `DATABASE_URL`.

So in-process builds without a PID namespace can re-sign contracts and reach the
database.

## Approved decisions to carry over

- **(a) Durable record.** The signed contract is stored at ingest in a new
  web-server database table, `trusted_contracts`. In-cluster that is Postgres;
  locally it is SQLite at `~/.aifactory/data.db`. It is added by an Alembic
  migration on head `c1f5a3d7b924`. The head was re-checked on `origin/dev`
  `d4e18ce0`: no revision has `down_revision = "c1f5a3d7b924"`.
- **(c) Isolation rule.** A trusted task is auto-merged only if its build was
  isolated. The web server stamps `build_isolation` on the record when it spawns
  the build, from its own state:
  - `kubejob` for a Job dispatch;
  - `sandbox-pidns` for an in-process spawn whose command really is bwrap with
    `--unshare-pid`;
  - `none` otherwise.

  The stamp is sticky: once `none`, it stays `none`. An empty stamp holds.
- **D2.** Trusted tasks ignore `satisfiedSystemGates` and
  `satisfied_system_gates` in `task_metadata.json`. A human merge clears a
  `system_gates` hold.
- **D3.** A held task hands off to TFactory with `contract={}`, so TFactory
  infers. A verified task hands off with the record, and a legacy task with
  `None` (today's path). The handoff never blocks.
- **D4-i.** "Isolated host" means the **web server's own environment at merge
  time** satisfies either:
  - `selected_backend() == "kubejob"`, or
  - `sandbox.is_enabled()` is true, `sandbox._mode()` is `fs` or `strict`, and
    `sandbox._pidns_enabled()` is true.

  On a host that is not isolated, `merge_disposition` returns `hold-blocking`
  for **every** task, legacy tasks included. The live cluster runs
  `AIFACTORY_BUILD_BACKEND=kubejob` (checked 2026-10-08), so production is not
  affected.
- **D5.** Trusted tasks already in flight at rollout have no record and no
  stamp, so they hold once. A human merges them or re-runs `/from-plan`. This
  goes in the release note.
- **Answer 1 (kid rotation).** There is no new code. `verify_plan_signature`
  reads the live keyring and retired kids at merge time, so a retired or
  removed kid holds.
- **Answer 5 (in-flight trace).** With no record, any of the following makes
  the task trusted:
  - `approval` in `context/task_contract.json` or in `implementation_plan.json`;
  - `provenance.trusted_plan is True` in `requirements.json`.

  Such a task holds unless its spec contract verifies by HMAC. Even then it
  holds under (c), because it has no stamp.
- **Out of scope:**
  - other `task_metadata.json` signals (a separate issue);
  - `deploy_scaffold.py` (a follow-up);
  - the env scrub (#1668/#1676, already merged);
  - the `/proc` exposure itself (a separate issue).

## Decision function (implemented in steps 4 and 7)

```
resolve_contract(spec_dir, trusted) -> (state, contract)
  trusted ∈ TrustedRecord | None | LOOKUP_FAILED
  LOOKUP_FAILED                                  -> hold
  record present:
     verify_plan_signature(record.contract) ok
     and record.build_isolation in {"kubejob","sandbox-pidns"}
     and canonical(spec_dir/context/task_contract.json) == canonical(record.contract)
                                                 -> verified, record.contract
     else                                        -> hold
  no record, trace present                       -> hold   (D5 + (c): no stamp)
  no record, no trace                            -> legacy, load_task_contract(spec_dir)

merge_disposition: if not host_isolated(): hold-blocking   (D4-i, first check, all tasks)
```

`canonical` is `trusted_plan._canonical` (`apps/backend/trusted_plan.py:162`).
Any exception inside `resolve_contract` returns `hold`, except in the legacy
branch, which keeps today's behaviour.

## Facts confirmed during planning

The spec asked for these to be confirmed; they are no longer open.

- **The spec directory is the main one on every path.** Ingest uses
  `project/.aifactory/specs/<id>` (`routes/execution.py:1391-1394`). Both
  completion entry points pass `spec_dir_for(project_path, spec_id)`:
  - `agent_service.py:428`/`:509` → `run_terminal_completion` at `:602`;
  - `agent_kubejob.py:252` → `:257`.

  The on-demand handoff route builds the same path (`execution.py:918-919`). So
  the record key is `sha256(str(Path(spec_dir).resolve()))` everywhere, with a
  single helper, `spec_key_for_dir(spec_dir)`.
- **There is one spawn site of each kind.**
  - In-process: every route (`agent_service.py:808`, `:907` via
    `_start_build_unit`, `agent_queue.py:90/139`, `agent_kubejob.py:429`
    fallback) ends in `_spawn_task_execution` (`agent_service.py:931`). That
    function has one `create_subprocess_exec` at `:1226`, right after
    `build_sandboxed_command` at `:1224`.
  - Kubejob: `KubeJobBuildBackend.dispatch` (`services/build_backend.py:1204`),
    called from `agent_kubejob.py:349`.
  - `agent_spec_creation.py:293` runs the spec pipeline, which the trusted fast
    path skips, so it is not stamped. A missed site leaves the stamp empty,
    which holds.

## Steps

The plan has 9 steps (step 0 syncs only). Each step is its own commit.

**Traps that apply to every commit:**

- The commit-msg scope must be a word, such as `merge`, `security`, `db` or
  `tfactory`, never `#1667`. The full subject must be **≤ 100 characters**
  (`.husky/commit-msg:37`, `:87`).
- Commit with `PATH="$PWD/apps/backend/.venv/bin:$PATH" git commit -F - <<'MSG'`,
  so that pre-commit's ruff and mypy resolve. End the message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01L5j1hQCdA4NmA4jiKDNBX4
  ```
- After any step that edits `apps/backend/**` or `apps/web-server/server/**`,
  run `apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py --check`.
  If it reports stale output, run it without `--check` and commit the
  regenerated `docs/docs/compliance/autonomy-matrix.md` and
  `docs/static/compliance/autonomy-matrix.json` **in the same commit**.
- Never put a path, a contract value or file content in a log line. Log constant
  strings only, and pass ids through `sanitize_log` (py/log-injection; see the
  pattern at `pr_endgame.py:1302-1305`).
- Never loosen an existing assertion in any existing test.

---

### 0. Sync the branch with dev

Run `git -C ../AIFactory-1667 merge --no-edit origin/dev`. The branch holds
only docs, so no conflict is expected. No file edits.

→ **Verify:**
- `git log --oneline -1 origin/dev` is an ancestor of `HEAD`;
- `git grep -n 'down_revision.*"c1f5a3d7b924"' -- apps/web-server/server/database/alembic/versions`
  prints nothing, so the head is unchanged. If it prints a file, use that
  file's `revision` as `down_revision` in step 2 instead.

Traps: no force-push. The approval commits are the audit trail.

### 1. Write the tests first (red)

Create or extend the following files. Each test is named `test_<n>_...` after
the spec's numbering, so the PR can be checked against it.

**`apps/web-server/tests/test_trusted_contract_merge.py`** (new): tests 1–6,
9–11, 14–20.

Fixtures:

- a `signed_plan` built with `trusted_plan.sign_plan` and a test key set by
  `monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY__T1", ...)`;
- a `spec_dir` under `tmp_path`, with `context/task_contract.json`,
  `requirements.json` and `task_metadata.json` written the way
  `ingest_trusted_plan` writes them. Call `ingest_trusted_plan` directly;
- an autouse fixture that sets `AIFACTORY_BUILD_BACKEND=kubejob` (an isolated
  host) unless the test overrides it.

Each test builds a `TrustedRecord(contract=..., build_isolation=...)` directly
and calls `merge_disposition(spec_dir, tier, trusted=...)`, or
`apply_path_risk_floor(..., trusted=...)`, or
`build_ingest_payload(..., contract=...)`.

| # | Arrange | Expect |
|---|---|---|
| 1 | Record stamped `kubejob`. Strip `deployment` from the spec contract (production plan). | `hold-blocking` |
| 2 | Record stamped `kubejob`. Delete the spec contract, `provenance.trusted_plan` and the plan's `approval`. | `hold-blocking` |
| 3 | Record stamped `kubejob`. Edit the spec contract and re-sign it with the test key. | `hold-blocking` |
| 4 | Record stamped `kubejob`, contract with `system_gates: ["human-approval"]`. Add `satisfiedSystemGates: ["human-approval"]` to `task_metadata.json`. | `hold-blocking` |
| 5 | Record stamped `kubejob`. Set `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS=pfactory/t1`. | `hold-blocking` |
| 6 | Change one character inside the record's `contract`. | `hold-blocking` |
| 9 | `trusted=LOOKUP_FAILED`. | `hold-blocking` |
| 10 | Record stamped `none`. | `hold-blocking` |
| 11 | Record with `build_isolation=None`. | `hold-blocking` |
| 14 | Record stamped `kubejob`, but `AIFACTORY_BUILD_BACKEND=subprocess` and `AIFACTORY_AGENT_SANDBOX` unset. | `hold-blocking` |
| 15 | Legacy task (no record, no trace) on that same host that is not isolated. | `hold-blocking` (D4-i) |
| 16 | Record stamped `kubejob`, untouched contract, low tier, not production. | `auto-merge` (guard) |
| 17 | Legacy task on an isolated host: (a) with no contract → same as dev (`auto-merge` for low); (b) with unparseable `context/task_contract.json` → `hold-blocking` (#1658). | guard |
| 18 | No record; `requirements.json` has `provenance.trusted_plan: true`; valid signed contract. | `hold-blocking` (D5) |
| 19 | `apply_path_risk_floor(..., trusted=<record stamped none>)` with enforcement on. | floor `blocking` |
| 20 | `build_ingest_payload(spec_dir, id, contract=record.contract)` with an edited spec contract. | `payload["contract"]` has the record's `deployment`, `tfactory` and the added `execution.phase_models`. With `contract={}` there is no `contract` key, or it is empty, and no exception. |

**`tests/test_trusted_contract_store.py`** (new, root suite): tests 7 (SQLite)
and 13.

- Point `DATABASE_URL` at `sqlite+aiosqlite:///<tmp_path>/t.db` and run
  `python -m alembic upgrade head` through `tests.postgres.helpers.run_alembic`
  (it runs with `cwd=apps/web-server`, which uses `alembic.ini`; `env.py:36-38`
  honours `DATABASE_URL`).
- **Test 7:** the table `trusted_contracts` exists, with the columns listed in
  step 2.
- **Test 13:** `put`, then `stamp_isolation("none")`, then
  `stamp_isolation("kubejob")` → `get().build_isolation == "none"`. Also,
  `stamp_isolation` on a missing row is a no-op.

**`tests/postgres/test_trusted_contracts_schema.py`** (new): test 7 on Postgres.
It skips cleanly without `TEST_DATABASE_URL` and follows
`tests/postgres/test_audit_resource_id_width.py`.

**`tests/test_from_plan_gate_ordering.py`** (extend): test 8.

- A signed plan → the row exists with the verbatim plan (store patched to an
  in-memory fake, or to the SQLite factory).
- The store's `put` raises → the response is 500 and `_start_build_unit` /
  `start_task_execution` was not called.

**`tests/test_trusted_contract_isolation_stamp.py`** (new): test 12.

- `_spawn_task_execution` with `shutil.which("bwrap")` patched to `None` and
  `create_subprocess_exec` faked → the store records `none`.
- With `build_sandboxed_command` patched to return
  `["/usr/bin/bwrap", ..., "--unshare-pid", ..., "--", "python", "run.py"]` →
  `sandbox-pidns`.
- `KubeJobBuildBackend.dispatch` with a fake batch API → `kubejob`, stamped
  before `create_namespaced_job` is awaited.

Follow `tests/test_agent_service_kubejob_backend.py` and
`tests/test_build_backend_kubejob.py` for the fakes.

→ **Verify:**
```
cd apps/web-server && ../backend/.venv/bin/pytest tests/test_trusted_contract_merge.py -q
cd ../.. && apps/backend/.venv/bin/pytest tests/test_trusted_contract_store.py tests/test_trusted_contract_isolation_stamp.py tests/test_from_plan_gate_ordering.py -q
```

Every test except 16 and 17 must fail, by import error or by assertion. Tests
16 and 17 may also fail at this point only because the `trusted=` keyword does
not exist yet. Record the failing list in the PR description.

Commit: `test(merge): failing tests for the signed-contract merge gate (#1667)`.

Traps:

- Test 15 sets `AIFACTORY_BUILD_BACKEND=subprocess` explicitly, so it does not
  depend on the developer's environment.
- `sandbox._bwrap_works` is `lru_cache`d (`sandbox.py:57`). Call
  `cache_clear()` in fixtures that change bwrap.

### 2. The table and the migration (a)

- **`apps/web-server/server/database/models.py`:** after `class JobState`
  (`:1123`, ends at about `:1220`), add `class TrustedContract(Base)` with
  `__tablename__ = "trusted_contracts"` and these columns:
  - `spec_key: String(64)`, primary key;
  - `spec_id: String(255)`, not null;
  - `contract: Text`, not null (JSON text);
  - `build_isolation: String(16)`, nullable;
  - `created_at` / `updated_at`, the same as on `JobState`.
- **`apps/web-server/server/database/__init__.py`:** export `TrustedContract`
  next to `JobState` (`:15`, `:39`).
- **New file
  `apps/web-server/server/database/alembic/versions/20261009_<12-hex>_trusted_contracts.py`:**
  - `revision = "<12-hex>"` (generate with `python -c "import uuid;print(uuid.uuid4().hex[:12])"`);
  - `down_revision = "c1f5a3d7b924"`;
  - `upgrade()` calls `op.create_table`; `downgrade()` calls `op.drop_table`;
  - the docstring is shaped like `20260620_b7e1c9a4d2f3_job_states.py`.

→ **Verify:** test 7 (SQLite) passes:
`apps/backend/.venv/bin/pytest tests/test_trusted_contract_store.py -q -k 7`.
The Postgres variant passes when `TEST_DATABASE_URL` is set, and skips when it
is not.

Commit: `feat(db): add the trusted_contracts table for signed task contracts (#1667)`.

Traps:

- `_verify_schema_at_head_sync` (`database/engine.py:83`) refuses to start
  against a database that is not migrated. Run the full migration test suite:
  `apps/backend/.venv/bin/pytest tests/postgres -q`, which skips without a
  database.
- Keep column names snake_case, as in every other table.

### 3. The store

New file: **`apps/web-server/server/services/trusted_contract_store.py`.**

- `TrustedRecord`: a frozen dataclass with `contract: dict` and
  `build_isolation: str | None`.
- `LOOKUP_FAILED`: a module-level sentinel object.
- `spec_key_for_dir(spec_dir) -> str`: `sha256(str(Path(spec_dir).resolve()))`.
- `class TrustedContractStore`, whose `__init__(session_factory=None)` lazily
  imports `async_session_factory`, exactly as `JobStateStore` does
  (`services/job_state_store.py:120-129`). Methods:
  - `async put(spec_key, spec_id, contract: dict)` upserts. It overwrites
    `contract`, keeps `build_isolation` **only if** the contract bytes are
    unchanged, and otherwise resets it to `None`. A re-ingest is a new build.
  - `async get(spec_key) -> TrustedRecord | None`.
  - `async stamp_isolation(spec_key, kind)`. It is a no-op if the row is
    missing. If `row.build_isolation == "none"`, the value is kept; otherwise
    it is set to `kind`.
  - Module-level `async lookup(spec_dir) -> TrustedRecord | None | LOOKUP_FAILED`.
    This wraps `get(spec_key_for_dir(spec_dir))`, and any exception becomes
    `LOOKUP_FAILED`, logged with a constant message.

→ **Verify:** test 13 passes:
`apps/backend/.venv/bin/pytest tests/test_trusted_contract_store.py -q`.

Commit: `feat(merge): async store for the trusted contract record (#1667)`.

Traps:

- Do not use `asyncio.run` anywhere. Every caller is already async (steps 5–8).
- Store the contract as `json.dumps(contract)`, not as the canonical form, so
  the handoff gets the verbatim plan. Equality is checked canonically in step 4.

### 4. The pure decision

New file: **`apps/web-server/server/services/trusted_contract.py`.** It is
synchronous and does no database I/O.

- `host_isolated() -> bool`, as defined in D4-i. It imports
  `build_backend.selected_backend` (`services/build_backend.py:358`) and
  `sandbox.is_enabled`, `_mode` and `_pidns_enabled`
  (`services/sandbox.py:49`, `:91`, `:106`).
- `has_trusted_trace(spec_dir) -> bool`, as defined in answer 5.
- `resolve_contract(spec_dir, trusted) -> tuple[str, dict]`, exactly as the
  decision function above. It imports `verify_plan_signature` and `_canonical`
  from `trusted_plan`, and `load_task_contract` from `pfactory.tfactory_client`,
  inside the function, as `pr_endgame` does (`:216-227`). An `ImportError`
  returns `("hold", {})`.

→ **Verify:**
- `cd apps/web-server && ../backend/.venv/bin/python -c "import server.services.trusted_contract"`;
- mypy passes through pre-commit.

Tests 1–6 still fail until step 7 is wired in, which is expected.

Commit: `feat(merge): resolve_contract decides verified, hold or legacy (#1667)`.

Traps:

- `context/task_contract.json` may hold any JSON. Compare
  `_canonical(json.loads(text))` to `_canonical(record.contract)` inside
  `try`, and treat any parse error as `hold`.
- Do **not** call `load_task_contract` for a trusted task. Its fallback to
  `implementation_plan.json` (`tfactory_client.py:103-106`) would hide a
  deletion.

### 5. Write the record at ingest (a)

**`apps/web-server/server/routes/execution.py`**, in `create_from_trusted_plan`
(`:1335`): straight after the `if not result.ok: raise ...` block that follows
`ingest_trusted_plan` at `:1433`, add:

```python
try:
    await TrustedContractStore().put(spec_key_for_dir(spec_dir), spec_id, request.plan)
except Exception:
    logger.error("[InstallPlan] trusted contract record not written; build not started")
    raise HTTPException(status_code=500, detail="Trusted contract record could not be stored")
```

This must run **before** any build start further down the function.

→ **Verify:** test 8 passes:
`apps/backend/.venv/bin/pytest tests/test_from_plan_gate_ordering.py -q`.
The existing tests in that file stay green.

Commit: `feat(merge): store the signed contract when a trusted plan is ingested (#1667)`.

Traps:

- On a 500 the spec directory is already allocated, the same as for any
  post-ingest failure. Do not add cleanup; it is out of scope.
- Run `gen_autonomy_matrix.py --check`, because `routes/execution.py` is a
  route module.

### 6. Stamp the isolation at spawn (c)

- **`apps/web-server/server/services/agent_service.py`**, in
  `_spawn_task_execution`: straight after
  `cmd = build_sandboxed_command(cmd, project_path)` (`:1224`) and **before**
  `create_subprocess_exec` (`:1226`), add:

  ```python
  head = cmd[: cmd.index("--")] if "--" in cmd else cmd
  kind = "sandbox-pidns" if head and head[0] == sandbox._bwrap_path() and "--unshare-pid" in head else "none"
  await TrustedContractStore().stamp_isolation(spec_key_for_dir(spec_dir), kind)
  ```

  `spec_dir` is the local variable from `:981`. Wrap the stamp in
  `try/except Exception` and log a constant message. A failed stamp leaves it
  empty, which holds, so it must not block the build.
- **`apps/web-server/server/services/build_backend.py`**, in
  `KubeJobBuildBackend.dispatch` (`:1204`): before
  `await batch.create_namespaced_job(...)` (about `:1282`), stamp `kubejob` for
  `spec_key_for_dir(spec_dir_for(project_path, spec_id))`, with the same
  try/except.

→ **Verify:** tests 12 and 13 pass:
`apps/backend/.venv/bin/pytest tests/test_trusted_contract_isolation_stamp.py tests/test_trusted_contract_store.py -q`.
The existing suites stay green:
`apps/backend/.venv/bin/pytest tests/test_agent_service_kubejob_backend.py tests/test_build_backend_kubejob.py tests/test_agent_service_durable_admission.py -q`.

Commit: `feat(merge): stamp how each trusted build was isolated at spawn (#1667)`.

Traps:

- Test `--unshare-pid` **only before `--`**, because the run.py argv after it
  is agent-adjacent input.
- Compare `head[0]` against the resolved `_bwrap_path()`, not the string
  `"bwrap"`.
- `build_sandboxed_command` returns the bare command when bwrap is missing
  (`sandbox.py:129`). That is exactly the case that must stamp `none`.

### 7. The merge path consumes it (D2, D4-i, #1658)

**`apps/web-server/server/services/pr_endgame.py`:**

- **`merge_disposition`** (`:459`) gets a **required** keyword,
  `trusted: object`.
  - First, after the imports: `if not host_isolated(): return HOLD_BLOCKING_DISPOSITION`
    with a constant warning (D4-i).
  - Then replace `:497-500` with `state, contract = resolve_contract(spec_dir, trusted)`.
  - `hold` → `HOLD_BLOCKING_DISPOSITION`, logged as "task contract not
    verifiable or build not isolated; auto-merge withheld".
  - `legacy` → keep the `_contract_unreadable` check (`:442-456`) and use
    `contract`.
  - `deployment = contract.get("deployment")`;
    `gates = satisfied_system_gates(spec_dir, deployment, trusted=(state == "verified"))`.
  - The import-failure hold (`:488-496`) also covers importing `trusted_contract`.
- **`satisfied_system_gates`** (`:318`) gets the keyword `trusted: bool = False`.
  When it is true, return only the contract's `deployment.satisfied_gates`, and
  skip the `task_metadata.json` read (`:331-340`) (D2).
- **`apply_path_risk_floor`** (`:190`) gets a required keyword, `trusted`. At
  `:252-260`:
  - `hold` → `floor = "blocking"`;
  - `verified` → use the record's deployment and `trusted=True` gates;
  - `legacy` → today's code.
- **`gather_pr_context`** (`:1113`) gets the keyword `trusted` and passes it at
  `:1218`.
- **`run_pr_endgame`** (`:1232`) gets the keyword `trusted` and passes it at
  `:1300`.

**`apps/web-server/server/services/completion_orchestration.py`**, in
`run_terminal_completion` (`:79`):

- Once, before the path-floor block that calls `apply_path_risk_floor` at
  `:219`, add `trusted = await trusted_contract_store.lookup(spec_dir)`.
- Pass `trusted=trusted` at `:219`, `:275` (`gather_pr_context`) and `:500`
  (`run_pr_endgame`).

→ **Verify:**
- tests 1–6, 9–11 and 14–19 pass;
- `cd apps/web-server && ../backend/.venv/bin/pytest tests/test_trusted_contract_merge.py tests/test_pr_endgame_merge_gate.py tests/test_pr_endgame_review_tier.py -q`;
- `apps/backend/.venv/bin/pytest tests/test_review_tier.py -q`.

Commit: `fix(merge): act only on the signed contract on the live merge path (#1667)`.

Traps:

- **Existing tests call `merge_disposition(spec_dir, tier)` and
  `apply_path_risk_floor(...)` without `trusted`, and run on a host that is not
  isolated.** Update every call site found by:
  ```
  git grep -n "merge_disposition(\|apply_path_risk_floor(\|gather_pr_context(\|run_pr_endgame(" -- apps/web-server/tests tests
  ```
  Each must pass `trusted=None` (legacy), and each such module gets an autouse
  `monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")`. **Do not change
  any expected disposition.** If an expectation would have to change, stop and
  report it as a deviation.
- `run_pr_endgame` runs the merge in a background task. `trusted` is a plain
  value captured before that task starts, so no session crosses the task
  boundary.
- The log line must not include the state's reason or any path.

### 8. The TFactory handoff sends the verified contract (D3)

- **`apps/backend/pfactory/tfactory_client.py`:**
  - `build_ingest_payload` (`:575`) gets the keyword
    `contract: dict | None = None`. At `:621`, use
    `contract = load_task_contract(spec_dir) if contract is None else dict(contract)`.
    The rest of the function (the `phase_models` and `github_issue` additions)
    is unchanged.
  - `maybe_auto_handoff_tfactory` (`:795`) gets the same keyword and passes it
    at `:818`.
- **`apps/web-server/server/routes/execution.py`**, in `handoff_to_tfactory`
  (`:879`): before `:927`, add
  `state, c = resolve_contract(spec_dir, await lookup(spec_dir))`, then
  `contract = c if state == "verified" else ({} if state == "hold" else None)`,
  and call `build_ingest_payload(spec_dir, spec_id, contract=contract)`.
- **`apps/web-server/server/services/completion_orchestration.py`:** compute
  the same `contract` from the `trusted` value of step 7, and pass `contract=`
  at `:237` and `:283`.

→ **Verify:**
- test 20 passes;
- `cd apps/backend && .venv/bin/pytest pfactory/ -q -k tfactory_client`
  passes (`test_tfactory_client_issue.py` and
  `test_tfactory_source_branch.py` are unchanged and green).

Commit: `fix(tfactory): hand off the verified contract, or none when held (#1667)`.

Traps:

- `load_task_contract` itself must not change. The prompt builders and
  `migration_mapper` still use it.
- On `hold`, log one constant warning, and never raise out of the handoff.

### 9. Docs and release note

- **`docs/docs/compliance/trusted-plan-key-rotation.md`:** add one sentence.
  Retiring or removing a kid holds the auto-merge of every trusted task signed
  with it that has not merged yet.
- **`CHANGELOG.md`, under Unreleased:**
  - the merge gate now verifies the signed contract and the build's isolation;
  - D4-i: on a host that is neither kubejob nor running a PID-namespaced
    sandbox, auto-merge is held for all tasks;
  - D5: in-flight trusted tasks hold once.
- **`docs/docs/environment-reference.md`:** under `AIFACTORY_BUILD_BACKEND` and
  `AIFACTORY_AGENT_SANDBOX_PIDNS`, add one line each on their effect on
  auto-merge.

→ **Verify:**
- `apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py --check` is
  clean;
- the docs build lints, if the pre-commit hooks cover it.

Commit: `docs(merge): document the signed-contract merge gate and its holds (#1667)`.

## Tests

| Spec test | File | Turns green at step |
|---|---|---|
| 1–6 tamper | `apps/web-server/tests/test_trusted_contract_merge.py` | 7 |
| 7 table at head | `tests/test_trusted_contract_store.py`, `tests/postgres/test_trusted_contracts_schema.py` | 2 |
| 8 record at ingest, 500 on failure | `tests/test_from_plan_gate_ordering.py` | 5 |
| 9 lookup fails → hold | `test_trusted_contract_merge.py` | 7 |
| 10, 11 stamp `none` / empty → hold | `test_trusted_contract_merge.py` | 7 |
| 12 stamps at spawn and dispatch | `tests/test_trusted_contract_isolation_stamp.py` | 6 |
| 13 sticky stamp | `tests/test_trusted_contract_store.py` | 3 |
| 14 host not isolated, record `kubejob` → hold | `test_trusted_contract_merge.py` | 7 |
| 15 D4-i legacy on a host that is not isolated → hold | `test_trusted_contract_merge.py` | 7 |
| 16, 17 guards (no over-holding; #1658 kept) | `test_trusted_contract_merge.py` | 7 (fail at step 1 only by signature) |
| 18 D5 in-flight → hold | `test_trusted_contract_merge.py` | 7 |
| 19 path floor `blocking` | `test_trusted_contract_merge.py` | 7 |
| 20 handoff `contract=` | `test_trusted_contract_merge.py` | 8 |

Full run before the PR:

```
cd apps/web-server && ../backend/.venv/bin/pytest tests -q -x
cd ../backend && .venv/bin/pytest pfactory/ -q
cd ../.. && apps/backend/.venv/bin/pytest tests -q -x
apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py --check
```

Expected: all green, with Postgres tests skipped unless `TEST_DATABASE_URL` is
set, and no existing assertion changed.

Runtime check on the dev cluster (kubejob):

1. Run `/from-plan` with a plan classified as production.
2. Strip `deployment` from the spec contract on the PVC.
3. Confirm the endgame logs the constant hold line and does not auto-merge.
4. Confirm `SELECT build_isolation FROM trusted_contracts WHERE spec_id = '<id>'`
   returns `kubejob`.

## Deviations

Recorded during implementation.

- **Step 1.** Test 20's `contract={}` case clears `phaseModels` in
  `task_metadata.json` first, because `phase_models` otherwise adds an
  `execution` block. This matches "no key, or empty".
- **Step 1.** Tests 16 and 17 fail at step 1 on the missing
  `trusted_contract_store` import as well as on the missing `trusted=` keyword.
- **Step 1, which constrains step 3.** The isolation-stamp tests patch
  `server.database.engine.async_session_factory`. `TrustedContractStore()` must
  resolve its session factory lazily from that attribute, as `JobStateStore`
  does.
- **Step 1, which constrains step 5.** The test-8 cases patch
  `execution_routes.TrustedContractStore`. `routes/execution.py` must import
  that name at module level.
- **Step 2.** `tests/postgres/test_audit_resource_id_width.py` pinned the
  Alembic head to `c1f5a3d7b924`. The new migration moves it, so the test now
  checks for a single head with `c1f5a3d7b924` in its chain. The original
  intent of the check is unchanged.
- **Step 5.** The existing `test_signed_plan_still_allocates_and_builds` has
  no database, so it now patches `execution_routes.TrustedContractStore` with
  an `AsyncMock` `put`. Its assertions are unchanged. Separately, the step 4
  `resolve_contract` was split into a `_record_verified` helper to satisfy
  strict ruff (PLR0911). Its behaviour is unchanged.
- **Step 6.** Both stamp sites call one helper,
  `trusted_contract_store.stamp_spawn`, instead of two inline `try/except`
  blocks. The behaviour is the same: a failed stamp stays empty, which holds.
  The step 5 `noqa: BLE001` was removed as unused (RUF100).
- **Step 7.** `scripts/gen_autonomy_matrix.py` and
  `tests/test_gen_autonomy_matrix.py`, which the plan did not list, now call
  the changed functions with `trusted=None` under
  `AIFACTORY_BUILD_BACKEND=kubejob`. The generated matrix therefore does not
  depend on the machine that regenerates it. `merger.py` passes
  `trusted=None`: the sweep only opens PRs and never merges, so a missing
  record can only make its context stricter. No existing expectation changed.
- **Step 8.** A `trusted_contract.handoff_contract(spec_dir, trusted)` helper
  maps verified, hold and legacy to the record's contract, `{}` and `None`, so
  the three handoff sites don't repeat the mapping. `apply_path_risk_floor`
  carries `noqa: PLR0913` for its sixth argument. The `build_ingest_payload`
  stub in `tests/audit/test_task_action_audit.py` accepts `**_k`, and no
  assertion changed.

## Rollback

- **Code:** `git revert` the commits from steps 3–8, newest first. The handoff,
  merge path and stamps go back to the dev behaviour together. Revert step 7
  and step 8 as a pair: step 8 depends on the `trusted` value that step 7
  threads through.
- **Schema:** the table is additive, and nothing reads it after the revert.
  Leave it, or run
  `cd apps/web-server && ../backend/.venv/bin/python -m alembic downgrade c1f5a3d7b924`
  to drop it. Revert the step 2 migration file only after that downgrade has
  run everywhere. A database stamped with a revision whose file no longer
  exists fails `_verify_schema_at_head_sync` on startup.
- **Effect of a rollback:** #1667 reopens. The #1658 holds and the #1668/#1676
  scrub are unaffected.
