---
status: draft
issue: 1673
intent: intent/2026-10-10-1673-unverified-task-contract.md
---

# Spec: deploy_scaffold and migration_mapper act only on a contract the server verified

## Design

The server already knows whether a contract is trusted: `resolve_contract()`
(`apps/web-server/server/services/trusted_contract.py:71-93`) applies #1667's full bar
(signature, isolation stamp, record matches disk, `_record_verified()` :49-60).
The build process cannot run that check. It has no database and must never hold
the key. So at every spawn the server turns its verdict into one environment
variable on `run.py`. That variable sits outside the spec dir, and the agent, a
child of `run.py`, cannot rewrite it. Both backend readers then go through one
small gate that hashes the on-disk contract and compares it with that value.

All paths below are under `apps/`.

### Decisions on the intent's open questions

1. **Scope.** Only `deploy_scaffold` and `migration_mapper`. One follow-up issue
   covers the prompt readers (`backend/prompts_pkg/prompts.py:376, :521, :603`) and
   `pfactory/tfactory_client.load_task_contract:87`, and points them at
   `core/contract_trust.trusted_contract()`. TFactory already gets the verified
   `handoff_contract` from the server.
2. **Ordering.** We do not wait for #1691/#1680. The change touches none of
   `trusted_plan.py`, `pfactory/tfactory_client.py`, `core/auth.py` or
   `core/client.py` (it only imports `_canonical`). The pod gets a digest and
   never the key. `host_isolated()` stays as it is.
3. **Source of trust.** A server channel: `AIFACTORY_TRUSTED_CONTRACT` holds
   `sha256(_canonical(contract))` when the state is verified and `hold` when it is
   held. It is unset when the state is legacy.
4. **Strength.** The full #1667 bar, because the value is minted only from
   `resolve_contract()` returning `verified`. On kubejob, `stamp_spawn` must run
   before the env is built, or `_record_verified` sees no `kubejob` stamp.
5. **Hold behaviour.**
   - Deploy: the scaffold is skipped and a `[trusted-contract]` warning is
     logged.
   - Migration: a held contract is never acted on. If it claims
     `change_mode=migration`, the build stops before the agent loop with the
     hold reason (`sys.exit(1)`, as at `backend/cli/build_commands.py:264, :520`).
     Otherwise the build runs as a normal build with a warning.
6. **Non-isolated hosts.** Whatever `resolve_contract` says, which is hold
   whenever there is a record, the lookup failed, or a trace exists with no
   record. With no server env at all (a local CLI run), the gate falls back to
   `has_trusted_trace`: hold if there is a trace, otherwise legacy.
7. **At completion.** A fresh read, hashed and compared with the env value,
   which is fixed for the life of the process. A contract that changes
   mid-build is never legitimate: a new contract comes only through ingest,
   which starts a new build (`trusted_contract_store.py:49-59`). A change
   therefore gives a mismatch, which holds.
8. **Deploy side.** This is defence in depth, kept because it costs one call.
   `_place()` never overwrites and the agent can write those paths itself. The
   gain is that a scaffold no longer looks contract-authorised.
9. **Resume or re-run.** Re-verified on every spawn from the DB row. Nothing is
   persisted in the spec dir.
10. **Observability.** A held contract produces a `logger.warning` plus a
    `print_status` line, both prefixed `[trusted-contract]` and carrying the
    reason (`held by server`, `digest mismatch`, or `trusted trace without a
    server verdict`). A held migration claim also exits non-zero, which already
    shows as a failed task. A task-status field is left to a follow-up.
11. **Tests.** Unit tests only (see Verification). A kubejob integration check is
    left to a follow-up.

### Backend: `backend/core/contract_trust.py` (new, about 40 lines)

This is the single gate for both readers, and the resolver the D1 follow-up
reuses. It is a new module because three places import it (the server and both
readers), and neither reader's module is a natural home for the others.
`trusted_plan.py` is left out because #1691 edits it.

- `ENV = "AIFACTORY_TRUSTED_CONTRACT"`
- `contract_digest(obj) -> str`, which is
  `sha256(trusted_plan._canonical(obj).encode()).hexdigest()`. The server and the
  pod both call this one function, so their hashes cannot drift apart.
- `has_trusted_trace(spec_dir)` is **moved** here from
  `web-server/server/services/trusted_contract.py:30-46`, together with its
  `_read_json` helper, with the body unchanged. The server module re-imports it,
  as it already imports backend modules (`:51`, `:74`). One predicate means the
  server's verdict and the pod's local fallback cannot disagree.
- `trusted_contract(spec_dir) -> tuple[str, dict | None, str]` returns
  (`verified`|`hold`|`legacy`, contract, reason):
  - It reads `context/task_contract.json` once through
    `migration_mapper.load_contract`. It then hashes that object and returns that
    same object, so the code never acts on a second read.
  - It reads `os.environ[ENV]` at call time. The table below gives the result for
    each case.

    | Env | Disk | Result |
    |---|---|---|
    | `hold` | any | hold, "held by server" |
    | a digest equal to `contract_digest(contract)` | parses as a dict | verified |
    | any other value, including empty, or missing/unreadable file | any | hold, "digest mismatch" |
    | unset | `has_trusted_trace` is true | hold, "trusted trace without a server verdict" |
    | unset | no trace | legacy: the contract or `None`, exactly as today |

  - Any exception gives hold, as at `trusted_contract.py:92`.
  - On hold the contract is returned only so that the migration caller can see
    what it claims. Callers never act on it.

`backend/core/migration_mapper.py:30-43`: `load_contract` keeps its signature.
Its docstring changes from "the signed Task Contract" to "the on-disk contract,
unverified; use `contract_trust.trusted_contract`".

### Backend readers

`backend/cli/build_commands.py:411-428`. Replace `_contract = load_contract(spec_dir)` with:

```python
state, _contract, why = trusted_contract(spec_dir)
if state == "hold":
    if is_migration(_contract):
        print_status(f"[trusted-contract] migration contract held ({why}); build stopped", "error")
        sys.exit(1)
    logger.warning("[trusted-contract] contract held: %s", why)
    print_status(f"[trusted-contract] contract held ({why}); normal build", "warning")
    _contract = None
```

The surrounding `except Exception` (:427) does not catch `SystemExit`, so the
deliberate exit goes through. A unit test pins this.

`backend/agents/deploy_scaffold.py:79-93`. Replace the raw `json.loads` with
`trusted_contract(spec_dir)`.
- On hold, log `[trusted-contract] deploy scaffold withheld: <why>` (both
  `logger.warning` and `print_status`) and return `[]`.
- Otherwise, call `scaffold_deploy(data.get("deployment") if data else None, ...)`.
- The function keeps its promise never to raise. `coder.py:1092` is unchanged.

### Server: compute the value at spawn

`web-server/server/services/trusted_contract.py` gets one helper beside `handoff_contract`:

```python
async def spawn_env(spec_dir: Path) -> dict[str, str]:
    """#1673: the server's verdict for run.py. A digest, never the key."""
    state, contract = resolve_contract(spec_dir, await lookup(spec_dir))
    if state == "verified":
        return {ENV: contract_digest(contract)}
    return {ENV: "hold"} if state == "hold" else {}
```

`web-server/server/services/build_backend.py` (kubejob):
- Move the `stamp_spawn` import and its call (:1281-1284) above
  `extra_env = build_job_env(oauth_token)` (:1259). The stamp is still written
  before `create_namespaced_job` (:1287), so #1667's rule that the record never
  lags the build still holds.
- Then add `extra_env.update(await spawn_env(spec_dir_for(project_path, spec_id)))`.
- `build_job_env` is a whitelist, so nothing else can inject `ENV`.

`web-server/server/services/agent_service.py` (in-pod and sandbox): directly
after `await stamp_spawn(...)` (:1235) and before `create_subprocess_exec`
(:1237), add `env.pop(ENV, None); env.update(await spawn_env(spec_dir))`.
- The stamp must come first. Otherwise a `none` stamp could follow a digest that
  was minted earlier.
- The `pop` stops a stray `AIFACTORY_TRUSTED_CONTRACT` in the server's own
  environment from reaching `run.py` as a fake verdict.
  `make_subprocess_env()` (`server/utils/subprocess_env.py:30`) copies `os.environ`.

## Alternatives rejected

- **Re-verifying the HMAC in the backend.** The key would have to be in the pod,
  which the constraint forbids (`trusted_plan.py:22,124`), and #1680 is removing
  it.
- **A verdict file written into the spec dir or worktree.** The agent can write
  there (the #1672 class of bug).
- **Importing `resolve_contract` into the backend.** It needs the DB and the web
  server, and kubejob pods have neither.
- **Passing the whole contract through env or a ConfigMap.** It is bigger and
  duplicates the record. The pod already has the bytes and only needs to know
  which bytes the server approved.
- **A copy of `has_trusted_trace` in the backend** (the minimal design). Two
  copies of a trust predicate would drift. Moving it costs the same number of
  lines.
- **A dedicated table row for each malformed env value, a `Verdict` class, a
  shared `log_hold` helper** (the robust design). A malformed value already
  fails the digest comparison and holds. Two log sites with different messages
  do not need a helper.
- **Raising inside `load_contract` on hold.** The `except Exception` at
  `build_commands.py:428` would swallow it as a debug line, which is the silent
  fallback the intent wants removed.
- **Setting `AIFACTORY_TRUSTED_CONTRACT=legacy` explicitly.** D3 leaves the
  variable unset. See the risk below.
- **Gating the prompt readers and `load_task_contract` now.** D1 moves that to
  a follow-up.

## Risks

- **Migrations stop on in-pod and sandbox hosts.** `host_isolated()` is true
  only for kubejob with a DB. On any other host, a trusted-plan migration with a
  record is held and exits 1, and its deploy scaffold is skipped. This is
  intended (D5, D6), but local and sandbox users will see the change until #1680
  lands. The release note must say so.
- **The `none` stamp is sticky** (`trusted_contract_store.py:75-90`). A build
  that ran unisolated once holds until the contract is re-ingested. The
  "held by server" reason points the operator there.
- **DB outage or a failed stamp.** `lookup` then returns `LOOKUP_FAILED`, or the
  stamp is missing, and the result is hold. Fail-closed is correct, but a DB
  hiccup now fails an unsigned-but-traced migration build, where before it only
  blocked auto-merge.
- **Stamp reorder on kubejob.** If the manifest build fails after the stamp, a
  `kubejob` stamp is left with no Job. That is harmless: the next spawn
  restamps. Moving the stamp back below `build_job_env` would silently hold
  every kubejob build. A test pins the order.
- **Residual gap: server says legacy, so the variable is unset.** The pod cannot
  tell "server said legacy" apart from "no server". An agent that writes a
  contract with no `approval` key and `change_mode: migration` in an earlier
  session gets a migration on re-run. The merge gate has the same gap, and the
  build had no signed contract to protect. Follow-up: emit an explicit `legacy`
  value.
- **Localized spec copy.** `run.py` reads the worktree copy
  (`build_commands.py:403-404`). The digest covers content, not path. A stale
  copy holds, so it fails closed.
- **Canonical form.** Both sides call `contract_digest`, which uses
  `_canonical`. If #1691 changes `_canonical`, both sides change together.
- **The digest is not a secret.** It shows in the Job spec. It is the hash of a
  contract the agent can already read.

## Verification

Unit tests (D11):

- **`tests/test_contract_trust.py`** (new) covers each row of the table:
  - verified, including a reformatted file (whitespace and key order)
  - `hold`
  - a mismatch
  - an empty or malformed env value
  - a missing file under a digest
  - a trace with no env
  - no env and no trace, which gives legacy
  - an exception, which gives hold
- **`tests/test_deploy_scaffold.py`**:
  - The existing cases stay unchanged. Their fixtures have no `approval` key
    (`:59`), so they resolve as legacy.
  - New cases:
    - A verified digest scaffolds.
    - `hold`, a mismatch, and a trace with no env each write nothing and log
      `[trusted-contract] deploy scaffold withheld`.
- **`tests/test_migration_mapper.py` / `build_commands`**:
  - A held contract that claims migration raises `SystemExit(1)` before the
    agent loop.
  - A held non-migration contract continues with `_contract=None`, and
    `prepare_migration_workspace` is never called.
  - A verified migration calls it.
- **`tests/test_trusted_contract_isolation_stamp.py`**:
  - `spawn_env` maps a verified record to its digest. Lookup failed, a `none`
    stamp, a mismatch, and a trace with no record each map to `{ENV: "hold"}`.
    Legacy maps to `{}`.
  - kubejob: `stamp_spawn` is awaited before `build_job_env`, and the manifest
    env carries the value (extend `test_12_kubejob_dispatch_stamps_before_job_is_created`, :130).
  - In-pod: a stray `ENV` in the server environment is removed when the state
    is legacy.
- **Cross-side:** the server's `contract_digest(record.contract)` equals the
  pod's verdict on the same dict written to disk with different formatting.

All tests set the variable with `monkeypatch.setenv` only, so the legacy
fixtures stay green.

```
cd /mnt/code/Source-home/GitHub/AIFactory-1673
pytest tests/test_contract_trust.py tests/test_deploy_scaffold.py tests/test_migration_mapper.py \
       tests/test_trusted_contract_isolation_stamp.py tests/test_trusted_contract_store.py \
       tests/test_constitution_prompt.py apps/backend/prompts_pkg/test_deployment_prompt.py \
       apps/web-server/tests/test_trusted_contract_merge.py -q
ruff check apps/backend/core/contract_trust.py apps/backend/core/migration_mapper.py \
       apps/backend/agents/deploy_scaffold.py apps/backend/cli/build_commands.py \
       apps/web-server/server/services/{trusted_contract,build_backend,agent_service}.py
git diff --name-only main -- apps/backend/trusted_plan.py apps/backend/pfactory/tfactory_client.py \
       apps/backend/core/auth.py apps/backend/core/client.py   # must print nothing (D2)
```

The change touches 7 source files (one of them new) plus tests. Under the
model-split rule, the approved plan therefore goes to the `coder` agent.

Follow-up issues to file:
- the sibling readers adopt `trusted_contract()` (D1)
- a task-status hold field (D10)
- a kubejob integration check (D11)
- an explicit `legacy` value (see Risks)
