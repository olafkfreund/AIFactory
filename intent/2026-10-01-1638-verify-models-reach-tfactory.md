---
status: draft
issue: 1638
author: olafkfreund
---

# Intent: the model chosen for verification is the model verification runs on

## Problem

`_verify_phase_models` (`apps/backend/pfactory/tfactory_client.py:120`) exists
to keep verification off the build's planning model. Its docstring:

> verification is judgment work (gen-functional / evaluator / planner / qa), so
> every TFactory phase uses the build's `qa` model (falling back to
> `planning`/`spec`) — keeping verify on the SAME provider as the build rather
> than silently falling back to TFactory's default sonnet.

It does not work. It carries its result on `contract.execution.phase_models`,
and **TFactory reads that field for exactly one phase.**
`apps/backend/agents/model_routing.py` (TFactory) has `routed_test_gen_model()`
and no equivalent for planning, spec, coding or qa. TFactory's planner resolves
its model from `phase_config.get_phase_model(phase, metadata)`, which reads
`metadata["phaseModels"]` in TFactory's **own** `task_metadata.json` — filled
by its ingest from the build's phaseModels verbatim.

So a build whose `planning` is Gemini gets its *verification* planned by Gemini.

## Measured

Task `025-myfriends-web-remediation-v2-o` was auto-handed to TFactory on
2026-10-01. Its planner died immediately:

```
Gemini CLI (yolo) error: "API key not valid. Please pass a valid API key."
  (API_KEY_INVALID)
planner: the session never reached a model (no observed model id recorded for
  the planning phase) — not retrying
```

Verification produced nothing. The build's planning model is Gemini by design;
the verify planning model should not have been.

AIFactory's build metadata for that task:

```json
{"coding": "claude-sonnet-4-6", "qa": "claude-sonnet-4-6",
 "qa_fixer": "claude-sonnet-4-5-20250929", "planning": "gemini",
 "test_gen": "claude-sonnet-4-6"}
```

TFactory's stored metadata for the same task:

```json
{"spec": "claude-sonnet-4-6", "planning": "gemini", "coding": "claude-sonnet-4-6",
 "qa": "claude-sonnet-4-6", "qa_fixer": "claude-sonnet-4-5-20250929"}
```

The same map. Had `_verify_phase_models` reached it, every phase would read
`claude-sonnet-4-6` — and the giveaway is `test_gen`, which that function
**always** sets and which is absent from what TFactory stored. So its output
never reached the field that decides the planner's model.

## Why nothing failed loudly

This is a guard written but not wired. The contract field is populated exactly
as the code says, and its one consumer (`routed_test_gen_model`) does read it.
So a unit test of `_verify_phase_models` passes, a unit test of
`routed_test_gen_model` passes, and the stated purpose — "every TFactory phase"
— is unmet for five phases out of six, with no test on either side positioned
to notice.

There is a second, independent problem in the same place. The merge at line 633
is:

```python
merged = {**verify_pm, **(execution.get("phase_models") or {})}
```

The incoming contract wins over the verify models. For a field whose entire
purpose is to override the build's choice for verification, that precedence is
backwards — and it would still defeat the function even after TFactory learns
to read the field, for any contract that already carries a `planning` entry.

## Proposed outcome

- The model `_verify_phase_models` chooses is the model TFactory's verify phases
  actually run on, for every phase it names and not only `test_gen`.
- A build on a provider whose credential is unusable for verification does not
  silently take verification down with it.
- The precedence is the one the function's purpose implies, so a contract
  carrying a build-side `planning` value does not override the verify choice.
- A test that would have caught this asserts on what TFactory **stores**, not
  on what AIFactory sends.

## Affected users and systems

- `apps/backend/pfactory/tfactory_client.py` — `_verify_phase_models` and the
  merge at line 633.
- TFactory's `agents/model_routing.py` and `phase_config.get_phase_model`, plus
  whatever its ingest uses to populate `task_metadata.json`. **The fix may
  belong on that side**, which makes this cross-repo.
- Every auto-handoff from a build whose phase models are not uniform.

## Constraints

- **Must not** silently repoint a deliberate per-phase choice. If a task really
  wants verification on a specific provider, that must remain expressible.
- The fix must be in **one** place, not a second parallel channel. There are
  already two paths carrying phase models to TFactory and the bug is that they
  disagree; adding a third would make it worse.
- Cross-repo changes need the vendoring rules respected — fix the canonical and
  re-vendor rather than patching a fork.
- Must not require a valid Gemini credential to be correct. The invalid key
  revealed this; it is not the cause.

## Open questions

1. **Which side owns the fix?** Teaching TFactory to read
   `contract.execution.phase_models` for every phase is one place and makes the
   existing field meaningful. Having TFactory's ingest prefer that field over
   the build's `phaseModels` is also one place. Changing AIFactory alone cannot
   fix it, since the field it writes is not consulted. I lean to the TFactory
   ingest, but it is the approver's call because it decides which repo this
   lands in.
2. **Should the merge precedence change regardless?** It is a latent defect
   even once (1) lands. Fix here, or as its own issue?
3. **Is "verify on the build's provider" still the right policy at all?** It was
   chosen so an Ollama build is verified on Ollama. An alternative is that
   verification always runs on a known-good judging model regardless of the
   build, which would have avoided this outright. That is a policy question
   above this bug, and worth asking while we are here.

## Related

- TFactory#1344 — the verification run that followed this one, whose findings
  were all environmental. Same task, different failure.
- #1619, Factory#1395 — earlier work on per-stage model configuration.
- The one thing that behaved correctly: TFactory refused to report a verdict it
  had not measured ("the session never reached a model ... not retrying")
  rather than a hollow pass.

Found driving olafkfreund/pfactory-friends-demo#113 through the factory.
