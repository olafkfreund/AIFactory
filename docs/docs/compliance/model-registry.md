---
title: Approved-model Registry
sidebar_position: 4
---

# Approved-model Registry

Which model runs each build stage (spec, planning, coding, qa, qa_fixer) is
freely configurable: `phase_config.DEFAULT_PHASE_MODELS`, a Task Contract's
`execution.phase_models`, or an `AIFACTORY_ROUTING_POLICY` tier can all point a
stage at a different model. That flexibility is also a gap (#323, #310): a model
can be swapped into the coding or QA seat with no record of its provenance and no
evaluation that it is fit for the job.

The approved-model registry (`apps/backend/model_registry.py`) is a declarative
allowlist that `phase_config` consults as an assertion after it resolves a
stage's model.

## Registry shape

Each entry is keyed by full Claude model id and records provenance, the version
the eval-gate signed off on, and the stages the model is approved for:

```json
{
  "claude-opus-4-8": {
    "provenance": "Anthropic",
    "version": "opus-4.8",
    "stages": ["coding", "planning", "qa", "qa_fixer", "spec"]
  },
  "claude-haiku-4-5-20251001": {
    "provenance": "Anthropic",
    "version": "haiku-4.5",
    "stages": ["qa"]
  }
}
```

The built-in `DEFAULT_REGISTRY` is kept in sync with the shorthands in
`phase_config.MODEL_ID_MAP`. Operators can override it with
`AIFACTORY_MODEL_REGISTRY` (inline JSON or a path to a JSON file, same
convention as the routing policy); an unset/unreadable/invalid value fails safe
to the default.

## Scope

The registry governs **first-party Claude models only** — the swap-risk that
matters for a hosted deployment. Provider-prefixed local or third-party models
(`ollama:*`, `openai:*`, `codex`, `gemini-*`, ...) are out of scope and always
pass, because their catalogs cannot be enumerated here. Govern those at the
provider-allowlist / gateway layer instead.

## Enforcement (default-safe)

`AIFACTORY_MODEL_REGISTRY_ENFORCE` selects the mode:

| Mode | Behaviour |
|---|---|
| `warn` (default) | Log a warning for an unregistered or mis-staged model; never block. Default builds use registered models, so this is silent in practice. |
| `deny` | Raise `ModelNotApprovedError` and fail the stage. Opt-in for locked-down deployments. |
| `off` | Skip the check entirely (byte-identical to pre-registry behaviour). |

The default is advisory, so wiring the assertion into `phase_config.get_phase_model`
changes nothing about which model a build picks — it only observes. A bug inside
the check itself is swallowed; only an explicit `deny` verdict blocks.

## The eval-gate: adding or swapping a model

A model must pass this gate **before** it is added to the registry (or before an
existing entry is pointed at a new version):

1. **Provenance recorded.** Provider, exact model id/version, and the change
   ticket. No model enters the registry without a named owner and a reason.
2. **Capability eval.** Run the model through the standard build-quality
   benchmark for the stage(s) it is proposed for (the campaign harness — see the
   benchmark program). Coding/planning models must clear the frontier bar;
   cheaper models may only be approved for `qa`/`qa_fixer`.
3. **Safety + injection eval.** Confirm the model honours the tool-use and
   prompt-injection guardrails at least as well as the incumbent.
4. **Cost/latency sign-off.** Record the per-token cost and typical latency so a
   swap cannot silently blow a budget.
5. **Staged rollout.** Register the model for the narrowest stage set that passed
   (e.g. `qa` only), enable it in `warn` mode first, watch, then widen.

The registry entry's `version` field pins exactly what the gate signed off on;
changing it is itself a change that must re-run the gate.
