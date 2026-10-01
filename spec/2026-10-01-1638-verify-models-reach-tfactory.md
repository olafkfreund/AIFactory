---
status: draft
issue: 1638
intent: intent/2026-10-01-1638-verify-models-reach-tfactory.md
---

# Spec: the verify models win the merge

## The intent's diagnosis was wrong, and this corrects it

The approved intent says `_verify_phase_models`'s output "never reached the
field that decides the planner's model", and offers `test_gen`'s absence from
TFactory's stored metadata as the giveaway. Reading the TFactory side to write
this spec showed both claims are false.

**`test_gen`'s absence proves nothing.** `TFactory
agents/tools_pkg/tools/task_control.py:472` translates
`contract.execution.phase_models` into TFactory's own `task_metadata.json`, and
copies exactly five keys:

```python
pm = {k: phase_models[k]
      for k in ("spec", "planning", "coding", "qa", "qa_fixer")
      if isinstance(phase_models.get(k), str)}
```

`test_gen` is deliberately not among them. Its absence downstream says nothing
about whether `_verify_phase_models` ran.

**The field is read, and the function did run.** The contract TFactory received
for spec `025-myfriends-web-remediation-v2-o`
(`context/task_contract.json`) is:

```json
{"spec": "claude-sonnet-4-6", "planning": "gemini", "coding": "claude-sonnet-4-6",
 "qa": "claude-sonnet-4-6", "qa_fixer": "claude-sonnet-4-5-20250929",
 "test_gen": "claude-sonnet-4-6"}
```

It carries `test_gen`, which **only** `_verify_phase_models` sets. So the
function ran, and its output reached the contract.

## The actual cause: the merge precedence

`apps/backend/pfactory/tfactory_client.py:633`:

```python
merged = {**verify_pm, **(execution.get("phase_models") or {})}
```

The incoming contract wins. For a field whose entire purpose is to override the
build's choice for verification, that is backwards.

Two observed values prove it rather than suggest it:

| key | `verify_pm` would set | the build had | observed |
| --- | --- | --- | --- |
| `planning` | claude-sonnet-4-6 | **gemini** | **gemini** |
| `qa_fixer` | claude-sonnet-4-6 | **claude-sonnet-4-5-20250929** | **claude-sonnet-4-5-20250929** |
| `test_gen` | claude-sonnet-4-6 | claude-sonnet-4-6 | claude-sonnet-4-6 |

`qa_fixer` is the build's value, not the qa model — so the incoming map won that
key too. `test_gen` is present only because the incoming map lacked it.
Computed both ways, `{**verify_pm, **incoming}` reproduces the observed map
**exactly**, and `{**incoming, **verify_pm}` yields the qa model for all six.

## Design

One line, reversed:

```python
merged = {**(execution.get("phase_models") or {}), **verify_pm}
```

Everything else follows: TFactory's ingest already copies the result into its
own metadata, and `get_phase_model` already reads that. No TFactory change is
needed.

## Open questions, as resolved

1. **Which side owns the fix: AIFactory.** The intent leaned to TFactory's
   ingest on the belief that the field was unread. It is read. The defect is
   entirely in AIFactory's merge, so the change lands here and TFactory is
   untouched.
2. **The precedence is the whole bug, not a latent extra.** The intent listed it
   as a separate concern to maybe file apart; it is the fix.
3. **"Verify on the build's provider" stays the policy.** It is deliberate — an
   Ollama build should be verified on Ollama — and this change makes it work as
   written rather than replacing it. Routing verification to a fixed judging
   model regardless of the build is a larger policy question and is not decided
   here. Worth noting the failure that exposed this was an invalid Gemini
   credential, not the policy.

## Alternatives rejected

- **Teach TFactory to prefer the contract over the build's metadata.** Nothing
  to fix: TFactory already derives its metadata from the contract. The wrong
  value is in the contract before TFactory sees it.
- **Drop `planning` from `_verify_phase_models`' output** so the build's choice
  survives. That is the current behaviour and the bug.
- **Add `test_gen` to TFactory's five copied keys.** Unrelated — `test_gen` is
  consumed from the contract directly by `routed_test_gen_model`, and it is
  already correct.
- **Fail loudly when the build's provider has no usable credential.** A real
  improvement and a different issue: it would have turned this into a clear
  error instead of an empty run, but it would not have put the right model in
  the field.

## Risks

- **A deliberate per-phase verify choice is now overridden.** If a caller
  intentionally sets `execution.phase_models.planning` on a contract meant for
  verification, `verify_pm` will now beat it. Accepted: `_verify_phase_models`
  only produces a map at all when the build declared `phaseModels`, and its
  documented contract is that every TFactory phase uses the build's `qa` model.
  A caller wanting something else should set the build's `qa` model, which is
  the single input this derives from.
- **Low blast radius.** The merge runs only in the auto-handoff path, only when
  the build declared `phaseModels`, and changes which model string a verify run
  uses — not what is executed or asserted.
- **No host risk.**

## Verification

1. **A unit test on the merge itself**, using the real observed data above: given
   the build's `phaseModels` and a contract already carrying
   `planning: gemini` and `qa_fixer: ...4-5`, the merged map must be the qa
   model for all six keys.
   **Mutation:** restore the original operand order — the test must fail, and
   must fail specifically on `planning` and `qa_fixer`, not merely differ.
2. **A test asserting what TFactory would store**, not only what AIFactory
   sends — the gap the intent's wrong diagnosis came from. Run the contract
   through the same five-key projection `task_control.py:472` applies and assert
   `planning` is the qa model. This is the assertion that would have caught the
   original defect.
3. **A regression test that `test_gen` is still set**, so a future change cannot
   satisfy (1) by dropping keys.
4. **Live proof:** hand off a task whose `phaseModels.planning` is a provider
   other than the build's qa provider, and read TFactory's stored
   `task_metadata.json` — `planning` must be the qa model. This is the check
   that distinguishes a fixed merge from a merge that merely looks right.
5. **Gates:** ruff, `ruff format --check`, `cq_ratchet.py` with CI's pinned
   ruff, the full `tests/` suite **and** the co-located `apps/backend/test_*.py`
   root, which CI runs as a separate step.

## Rollback

Revert the one line. Verification returns to inheriting the build's planning
model. Nothing persists: the merge is computed per handoff.
