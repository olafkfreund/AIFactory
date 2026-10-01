---
status: draft
issue: 1638
spec: spec/2026-10-01-1638-verify-models-reach-tfactory.md
---

# Plan: the verify models win the merge

## Approved decisions (self-contained)

- **The intent's diagnosis was wrong and the spec corrected it.** It is not that
  `_verify_phase_models`' output never reached TFactory. TFactory
  `agents/tools_pkg/tools/task_control.py:472` *does* translate
  `contract.execution.phase_models` into its own `task_metadata.json`, copying
  exactly `("spec","planning","coding","qa","qa_fixer")` and deliberately
  excluding `test_gen`. So `test_gen`'s absence downstream proves nothing — it
  was a deliberate projection read as a missing write.
- **The function ran and arrived.** The contract TFactory received for spec
  `025-myfriends-web-remediation-v2-o` carries `test_gen: claude-sonnet-4-6`,
  and nothing but `_verify_phase_models` sets that key.
- **The cause is the merge precedence** at
  `apps/backend/pfactory/tfactory_client.py:633`:
  `merged = {**verify_pm, **(execution.get("phase_models") or {})}` — the
  incoming contract wins, which is backwards for a field whose purpose is to
  override the build's choice for verification.
- **Proven, not argued.** `qa_fixer` in the observed map is the *build's*
  `claude-sonnet-4-5-20250929`, not the qa model, so the incoming map won that
  key too. Computed both ways, the current order reproduces the observed map
  **exactly**; the reverse yields the qa model for all six keys.
- **The fix is one reversed line, in AIFactory. TFactory is untouched** — the
  opposite of the intent's conclusion on open question 1.
- **"Verify on the build's provider" stays the policy.** This makes it work as
  written. Routing verification to a fixed judging model is a larger question,
  not decided here.
- **Accepted risk:** a caller who deliberately sets
  `execution.phase_models.planning` on a verification contract is now
  overridden. `_verify_phase_models` only produces a map when the build declared
  `phaseModels`, and its documented contract is that every TFactory phase uses
  the build's `qa` model; a caller wanting otherwise sets that.

## Steps

Branch `fix/1638-verify-models-reach-tfactory` off `dev` (already created).
One file changes, so per the model-split rule this is implemented in-session
rather than handed to a coder.

1. **Reverse the merge** in `tfactory_client.py`:
   `merged = {**(execution.get("phase_models") or {}), **verify_pm}`, with a
   comment stating why the order matters and naming the `qa_fixer` evidence.
   → verify: step 2's test.
2. **A unit test on the merge, using the real observed data.** Given the build's
   `phaseModels` (`planning: gemini`, `qa_fixer: claude-sonnet-4-5-20250929`,
   rest `claude-sonnet-4-6`) and a contract already carrying that same map, the
   merged `execution.phase_models` must be `claude-sonnet-4-6` for all six keys.
   → verify: fails before step 1, passes after.
   **Mutation:** restore the original operand order — the test must fail **and
   name `planning` and `qa_fixer` specifically**, not merely report inequality,
   so a future reader sees which keys carry the property.
3. **A test asserting what TFactory would store**, not only what AIFactory
   sends. Apply the same five-key projection `task_control.py:472` uses to the
   merged map and assert `planning` is the qa model. This is the assertion whose
   absence produced the wrong diagnosis, so it is the one that matters most.
   → verify: fails before step 1, passes after.
4. **A regression test that `test_gen` is still present** in the merged map, so
   a future change cannot satisfy steps 2-3 by dropping keys.
5. **Gates:** `ruff check`, `ruff format --check` with CI's pinned ruff,
   `scripts/cq_ratchet.py` (not bare ruff — CI's strict baseline is what
   gates, and running plain `ruff` has already produced a false "green" in this
   repo), the full `tests/` suite, **and** the co-located
   `apps/backend/test_*.py` root, which CI runs as a separate step and which a
   change of mine has passed `tests/` while failing.
6. **Live proof.** Hand off a task whose `phaseModels.planning` names a provider
   other than its `qa` provider, then read TFactory's stored
   `specs/<id>/task_metadata.json`: `planning` must be the qa model. This is
   what distinguishes a fixed merge from one that only looks right — and it is
   the exact artefact that exposed the bug.
7. **PR → `dev`** linking intent, spec and plan; close #1638. The PR records the
   intent's corrected diagnosis so the history does not read as if the first
   analysis stood.

## Tests

```sh
V=apps/backend/.venv/bin
$V/python -m pytest tests/ -q -k "verify_phase_models or tfactory_client"
$V/python -m pytest tests/ -q
(cd apps/backend && ../../$V/python -m pytest . -q)   # the co-located root CI runs
$V/ruff check apps/backend apps/web-server tests scripts
$V/ruff format --check apps/backend apps/web-server tests scripts
$V/python scripts/cq_ratchet.py --base origin/dev
```

Expected: steps 2-4 fail before step 1 and pass after; the operand-order
mutation fails on `planning` and `qa_fixer`; the live handoff stores the qa
model for `planning`.

## Rollback

Revert the one line. Verification returns to inheriting the build's planning
model. Nothing persists — the merge is computed per handoff, and contracts
already sent keep whatever they were stamped with either way.
