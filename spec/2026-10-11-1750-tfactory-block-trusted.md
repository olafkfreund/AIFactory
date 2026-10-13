---
status: draft
issue: 1750
intent: intent/2026-10-11-1750-tfactory-block-trusted.md
---

# Spec: the TFactory test profile must come from the trusted contract

## Design

One change of source on the `start_task` TFactory path. Then delete the
unchecked reader, which has no other caller.

### 1. `start_task` reads the profile from the web server's verdict

`apps/web-server/server/routes/execution.py`, TFactory branch of `start_task`
(currently `_tf = load_tfactory_block(spec_dir)`, line 366):

```python
_state, _contract = resolve_contract(spec_dir, await lookup(spec_dir))
_tf = _contract.get("tfactory") if isinstance(_contract, dict) else None
_tf = _tf if isinstance(_tf, dict) else {}
```

`resolve_contract` (`server/services/trusted_contract.py:67`) already gives the
three answers the approver chose, so no new helper is needed:

| State | When | Contract returned | `tfactory` sent |
| --- | --- | --- | --- |
| `verified` | isolated host, signed record, stamp `kubejob`/`sandbox-pidns`, disk copy matches | the record's signed contract | the signed block |
| `hold` | `LOOKUP_FAILED`, record that does not verify, non-isolated host with a record, trusted trace with no record, any exception | `{}` | `{}`: the handoff is still sent, and TFactory infers its own plan (decision 1) |
| `legacy` | no record and no trusted trace | `load_task_contract(spec_dir)`: `context/task_contract.json` first, then `implementation_plan.json` | that file's block, if any (decision 2) |

- `lookup` (`trusted_contract_store.py:95`) never raises. It returns
  `LOOKUP_FAILED`, which holds. `resolve_contract` catches every exception and
  holds. So the route's narrow `except (JSONDecodeError, OSError, ImportError)`
  cannot turn a trust failure into a coder run.
- Imports: add `resolve_contract` to the existing
  `from server.services.trusted_contract import handoff_contract` line.
  `lookup` is already imported at the top of `execution.py`. Remove
  `load_tfactory_block` from the in-function `pfactory.tfactory_client` import.
- `handoff_contract` is not used here. It returns `None` for legacy, so the
  route would need a second `load_task_contract` call. `resolve_contract`
  already makes that call.
- Nothing else changes in the route: `build_handoff_payload`, `send_handoff`,
  the `TFACTORY_HANDOFF.md` marker or the response. On hold, add one
  `logger.warning` that names the task and says the declared profile was
  withheld. The intent requires the tamper case to be visible, and today it is
  silent. The log names only the state and never the contract's content.

The verified block comes from the record's own contract. It does not come from
`implementation_plan.json`, which the build rewrites (and which can lose
`tfactory`), or from `context/task_contract.json`, which `_record_verified` has
only compared against the record.

### 2. Delete `load_tfactory_block` (decision 3)

Its only caller is the line above (`grep -rn load_tfactory_block` at the branch
head). Remove:

- the function and its `__all__` entry in `apps/backend/pfactory/tfactory_client.py`
  (lines 40 and 69-85);
- the import and `__all__` entry in `apps/backend/pfactory/__init__.py`
  (lines 25 and 43);
- the import and the three pinning tests in `tests/test_pfactory_tfactory_client.py`
  (line 15, lines 69-87).

No docs or autonomy-matrix file names the function, so nothing is regenerated.
The Factory#943 matrix cells are not touched.

### 3. Tests

- `tests/test_pfactory_routing.py`: stub `exec_mod.lookup` (an async fake) in
  the two existing `start_task` TFactory tests, so they do not depend on a
  database. Add two cases that capture the payload through the existing
  `fake_poster` pattern:
  - **hold / tamper**: the spec has a trusted trace (`"approval"` in
    `context/task_contract.json`) and an `implementation_plan.json` with an
    edited `tfactory` block. `lookup` returns `None`. Expect the handoff to be
    sent with `payload["tfactory"] == {}`.
  - **legacy**: no trace. `context/task_contract.json` carries a `tfactory`
    block (no `approval`), and `lookup` returns `None`. Expect that block in the
    payload.
- `apps/web-server/tests/test_trusted_contract_merge.py`, next to `test_20`:
  this file already has the signing fixtures (`_spec`, `_rec`, isolated-host
  autouse). Add a **verified** case. Sign a contract with a `tfactory` block,
  then overwrite `implementation_plan.json` with a different block. Patch
  `server.routes.execution.lookup` to return the record and call `start_task`.
  Expect the signed block in the payload, not the edited one. Add one
  **non-isolated** case (`AIFACTORY_BUILD_BACKEND=subprocess`) that expects
  `{}`. This pins decision 4.

### 4. Housekeeping

- `CHANGELOG.md`, `## [Unreleased]` → `### Security`: one entry for #1750. It
  says trusted-plan tasks on hosts that cannot verify now hand off with no
  declared profile.
- File the follow-up issue to widen `host_isolated` now that #1680 has landed
  (decision 4). The docstring at `trusted_contract.py:33` still says "Revisit
  when #1680 lands". Link the new issue from the PR. The code is not changed
  here.
- The branch is already rebased on dev after PR #1751 merged
  (`git merge-base --is-ancestor` confirms it), so decision 5 is met.

## Alternatives rejected

- **`core.contract_trust.trusted_contract` (the issue's suggestion).** It reads
  `AIFACTORY_TRUSTED_CONTRACT` from the pod's environment. The web server never
  has that variable, so this reader can only return hold or legacy there and
  can never return verified.
- **`handoff_contract(...)` plus a `load_task_contract` fallback when it returns
  `None`.** This gives the same result with two calls instead of one.
  `resolve_contract` already holds the legacy branch.
- **Skip the handoff on hold.** The approver rejected this (decision 1). TFactory
  still gets the spec and infers its own plan.
- **Keep `load_tfactory_block` for legacy.** The approver rejected this
  (decision 2). It reads only `implementation_plan.json`, which the build
  rewrites. `load_task_contract` reads the build-safe copy first.
- **Gate inside `build_handoff_payload`.** That function is pure and sits in
  `pfactory` (backend). The verdict needs the web server's store and host
  check. Moving the gate there would bring web-server imports into the backend
  package.
- **Widen `host_isolated` in this change.** This is out of scope (decision 4).
  It changes the merge gate and the path floor too, so it gets its own issue.

## Risks

- **Trusted-plan tasks lose their declared profile on hosts that cannot verify.**
  Any host that is not kubejob with `DATABASE_URL` (a subprocess backend, a
  sandbox-pidns host, or a dev box) now sends `tfactory: {}` for those tasks,
  and TFactory infers instead. The approver accepted this, and the CHANGELOG
  states it. Legacy tasks are not affected.
- **The store is unreachable on an isolated host.** `LOOKUP_FAILED` holds, so
  even signed tasks send `{}` until the database is back. This is the same
  behaviour as `handoff_to_tfactory` and the merge gate. It fails safe.
- **Legacy still trusts the files.** A task with no record and no trace keeps
  the old trust level: `load_task_contract` falls back to
  `implementation_plan.json`. An agent that strips every trace from a task
  that never had a record is the existing D5 limit shared by all readers. It is
  not new here, and a task with a record cannot downgrade to legacy because the
  record takes precedence.
- **One more DB round trip on `start_task`** for TFactory-routed specs only.
  It is a single primary-key `select`, so the cost is negligible.
- **Merge overlap.** PR #1752 edits `execution.py` away from line 366. A
  textual conflict on the import line is possible, and the fix is to rebase.

## Verification

- `grep -rn load_tfactory_block apps tests` returns nothing.
- `pytest tests/test_pfactory_routing.py tests/test_pfactory_tfactory_client.py apps/web-server/tests/test_trusted_contract_merge.py`
  passes. The new hold test fails on the current code, because it receives the
  edited block. This proves the regression is pinned.
- The full backend and web-server test suites and `ruff`/`mypy` as run in CI
  are green.
- Repeat the intent's read-only probe on a kubejob host after deploy. Edit
  `implementation_plan.json`'s `tfactory` block on a trusted spec and start
  it. The payload carries the signed block, or `{}` with the warning logged if
  the record does not verify. It never carries the edited block.
