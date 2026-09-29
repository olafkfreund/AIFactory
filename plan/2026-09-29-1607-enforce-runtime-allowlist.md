---
status: approved
issue: 1607
spec: spec/2026-09-29-1607-enforce-runtime-allowlist.md
---

# Plan: derive the vocabulary, gate at the factory, populate before enforcing

Self-contained summary of the approved decisions.

## Approved decisions (carried from the spec)

- **The vocabulary is derived, not a second hand-maintained set.** `KNOWN_RUNTIMES`
  and the factory registries have already drifted: `copilot`, `github-models`,
  `openai-compatible` and `opencode` are producible by the live path and absent
  from the gate's list, so enforcing today would make them *permanently*
  unreachable — `operator_allowlist()` drops unrecognised tokens, so no operator
  value could re-enable them.
- **Tokens normalise through the factory's alias table**, so
  `AIFACTORY_RUNTIMES=gemini` means `antigravity` rather than nothing.
- **One chokepoint**: `providers/factory.py`, at `get_provider` (`:297`) and
  `get_qa_llm_provider` (`:357`), after the canonical is resolved. Called
  explicitly at both, not hidden inside `_apply_endpoint_defaults` (`:174`).
  **No signature changes** — the gate needs the canonical and the environment,
  and is not phase-parameterised.
- **Populate before enforcing.** Day one:
  `AIFACTORY_RUNTIMES=codex,antigravity,openai-compatible,copilot`. `claude` is
  always enabled and is not listed.
- **`AIFACTORY_RUNTIMES` joins `_PASSTHROUGH_BUILD_ENV`** — otherwise every
  dispatched build reads an empty allowlist and gates everything to claude.
- **`qa/loop.py:628` passes `gated=True`** — without it failover picks a gated
  runtime and takes a raise from the factory, turning graceful degradation into
  an exception.
- **Unknown tokens raise**, reversing the module's current rationale: the
  realistic operator error is a typo silently *disabling* what they meant to
  enable. Safe because the call is lazy at provider construction — a gitops typo
  costs one build, not the pod.
- **Signed contracts carry `runtime`**: `_EXECUTION_TO_METADATA` gains the key.
- **#790 closes here**: `copilot` becomes representable and allowlisted. No
  provider, credential or image work — all verified present.
- **Scoped out, stated not implied**: `claude` never reaches the factory (every
  call site short-circuits to `create_client`), so `claude-subagents` and
  `dynamic-workflow` — both mapping to `"claude"` — are not enforceable at this
  seam. Separate issue.

## Verified facts this plan relies on

- `runtime_gating.py`: `DEFAULT_RUNTIME = "claude"` (`:37`), `KNOWN_RUNTIMES`
  (`:42-52`), `MANUAL_ENABLE_ONLY = {"claude-subagents", "dynamic-workflow"}`
  (`:57`), `ALLOWLIST_ENV = "AIFACTORY_RUNTIMES"` (`:62`), `operator_allowlist`
  (`:76-91`), `is_runtime_enabled` (`:94`), `RuntimeNotEnabledError` (`:119-140`,
  message shape already right), `resolve_runtime` (`:143`).
- `providers/factory.py`: `_AGENTIC_REGISTRY` (`:57`), `_TEXT_REGISTRY` (`:84`),
  `_PROVIDER_ALIASES` (`:105`), `_resolve_canonical` (`:162`),
  `_apply_endpoint_defaults` (`:174`), `_RUNTIME_TO_PROVIDER` (`:244`),
  `get_provider` (`:297`), `get_qa_llm_provider` (`:357`).
- Set difference, measured: factory canonicals are `antigravity claude codex
  copilot github-models ollama ollama-cloud openai-compatible opencode`;
  `KNOWN_RUNTIMES` is `antigravity claude claude-subagents codex dynamic-workflow
  ollama ollama-cloud`. Four missing, two extra (the claude-mapped pair).
- `operator_allowlist` has one real caller, `provider_failover.py:94`
  (`enabled_failover_chain`), reachable only via `next_provider(..., gated=True)`;
  the production call at `qa/loop.py:628` omits the keyword.
- `_DEFAULT_CHAIN = ("claude", "codex", "antigravity")` (`provider_failover.py:31`).
- Eight live call sites route `infer_provider_from_model → get_provider`:
  `coder.py:456-482`, `planner.py:102-122`, `qa/loop.py:231-248,:531-561`,
  `qa/correction.py:184`, `spec/pipeline/agent_runner.py:162`,
  `agents/parallel_integration.py:456`, plus `get_qa_llm_provider`
  (`qa/loop.py:96,106,115,137,141`).
- History: 479 task files on the live PVC, zero non-Claude models, zero `runtime`
  fields. Exposure is latent (web UI picker) and automatic (failover), not
  historical.
- Copilot: provider wired (`factory.py:62,90`, aliases `:132-134`), model-string
  routing (`phase_config.py:771-775`), credential live (`factory-cli-creds` key
  `copilot-apps.json`, 152 B, well-formed), CLI in the pod (`1.0.88`).

## Steps

1. `tests/` — **write the vocabulary-parity test first**: every factory canonical
   is in the derived set, and the derived set adds only the two claude-mapped
   runtimes. → verify by it failing on `dev` for the four missing tokens, which
   is the regression test for the drift that caused this issue.

2. `apps/backend/core/runtime_gating.py:42-52` — derive `KNOWN_RUNTIMES` from
   `providers.factory`'s registries and alias table plus `MANUAL_ENABLE_ONLY`,
   instead of the literal. Import lazily if a module-level import would cycle
   (`factory.py` already imports this module at `:45`) — check and state which.
   → verify by step 1.

3. `runtime_gating.py:76-91` — normalise tokens through the factory alias table
   before membership, and **raise** on a token that resolves to nothing, naming
   the variable and the token. Docstring replaces the "a typo can never enable an
   unintended runtime" rationale with the reason for reversing it. → verify by a
   test that `gemini` enables `antigravity` and that `AIFACTORY_RUNTMIES`-style
   garbage raises.

4. `apps/backend/providers/factory.py:297` and `:357` — call the gate explicitly
   after the canonical is resolved, before construction. → verify by tests that a
   non-allowlisted runtime raises `RuntimeNotEnabledError` and an allowlisted one
   constructs.

5. `apps/backend/qa/loop.py:628` — pass `gated=True` to `next_provider`. → verify
   by a test that a chain containing a disabled runtime skips it rather than
   raising.

6. `apps/web-server/server/services/build_backend.py:256` — add
   `AIFACTORY_RUNTIMES` to `_PASSTHROUGH_BUILD_ENV`. → verify by a test asserting
   it appears in a dispatched Job's env.

7. `apps/backend/trusted_plan.py:701-724` — add `"runtime": "runtime"` to
   `_EXECUTION_TO_METADATA`. → verify by a test that an `execution.runtime`
   survives `execution_profile_to_metadata`.

8. `tests/` — the rest of the spec's verification list: `claude` unaffected with
   the allowlist unset/set/empty (the regression net for the whole fleet's actual
   workload), copilot end to end, and the enforcement matrix. → verify by the
   commands below.

9. Commit, then both halves of cq-ratchet (it diffs committed history). → verify
   by "0 regressed" from each.

10. **gitops, applied separately and only with explicit go-ahead**: set
    `AIFACTORY_RUNTIMES=codex,antigravity,openai-compatible,copilot` on the
    aifactory Deployment. **Must not be applied before the code is deployed** —
    on the current image the value is inert, and after the code lands without it
    the allowlist is claude-only, which would break QA failover. → verify by the
    ordering note below.

11. Open the PR against `dev` linking all three artifacts, noting it closes #790.

## Ordering — the part that can cause an outage

The code is safe to merge alone: with `AIFACTORY_RUNTIMES` unset,
`operator_allowlist()` returns `{claude}`, which is 100% of observed usage — but
QA failover to codex/antigravity would start being refused. So:

1. merge the code,
2. deploy it,
3. **then** apply the gitops value,

and treat the window between 2 and 3 as one where failover degrades to
claude-only. If that window is unacceptable, apply the gitops value first: it is
inert until the code ships, so there is no state in which both are wrong.
**Recommended: gitops first**, precisely because it is inert.

## Tests

```bash
apps/backend/.venv/bin/pytest tests/ -k "runtime_gating or provider or failover or trusted_plan" -v
apps/backend/.venv/bin/pytest tests/ -m "not slow" -q
```

Expected: the parity test fails on `dev` and passes after step 2; enforcement
tests pass; the existing provider/failover suites pass unchanged; cq-ratchet
"0 regressed" on both halves.

Ratchet note: it counts TID252, untyped defs, PLC0415 and DTZ006. If step 2 needs
a lazy import to break a cycle, it will trip PLC0415 — add a `noqa` naming the
cycle, matching the convention in `build_backend.py`.

## Rollback

`git revert <sha>` on the squash-merged commit, and remove the gitops line. No
schema, no migration. Reverting restores ungated selection; the gitops line alone
is inert without the code, so removing either independently is safe.

## Out of scope

- Gating `claude-subagents`/`dynamic-workflow` — not reachable at this seam.
- Restricting `StartTaskRequest.model` at the API boundary. The allowlist bounds
  which runtimes can be *used*; bounding what can be *asked for* is a different
  control.
- Proving a Copilot build completes against the seeded credential — needs a paid
  run; #790's acceptance, not this plan's.
