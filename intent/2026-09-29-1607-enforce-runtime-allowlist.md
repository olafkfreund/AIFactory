---
status: draft
issue: 1607
author: Olaf Krasicki-Freund
---

# Intent: the runtime allowlist must gate something, or stop existing

## Problem

`core/runtime_gating.py` implements an operator allowlist: `AIFACTORY_RUNTIMES`
names which runtimes a contract may select, `KNOWN_RUNTIMES` bounds the valid
set, and "speed-up" runtimes are excluded even from a broad `all` token because
they multiply spend. None of it is enforced.

**`get_runtime_provider()` has no production caller.** A repo-wide search finds
its definition (`providers/factory.py:268`), its `__all__` entry (`:488`), a
docstring reference in `core/provider_failover.py:92`, and tests. Nothing else.
The path actually taken is `agents/coder.py:456-482` →
`phase_config.infer_provider_from_model()` → `providers.factory.get_provider()`,
which never consults `runtime_gating`. The same shape appears in
`planner.py:102-122` and the QA paths (`qa/loop.py:231-248`, `:531-561`).

So a model string of the form `<runtime>:<model>` selects a provider directly
(`phase_config.py:771-775`) with nothing in the way, and `StartTaskRequest.model`
is an unrestricted string (`execution.py:59-70`) on an endpoint that requires
only task-member access (`:257-263`).

**Signed contracts are worse off than unsigned ones.** `_EXECUTION_TO_METADATA`
(`apps/backend/trusted_plan.py:701-724`) maps `model`, `phase_models`,
`phase_thinking`, `parallel`, `workers`, `complexity`, `review_tier`, `skills`,
`skip_planning`, `autonomy_tier` and `budget_usd` — and has **no `"runtime"`
key**. A Task Contract v2 `execution.runtime` is silently discarded, so even a
correctly wired gate would not see what a signed contract asked for.

**And the allowlist could not reach the coder anyway.** `AIFACTORY_RUNTIMES` is
absent from `_PASSTHROUGH_BUILD_ENV` (`build_backend.py:256-320`), and
`AIFACTORY_BUILD_BACKEND=kubejob` is live, so the coder runs in a Job whose env
would never carry it.

The cost of the present state is not that a control is missing — it is that a
control *appears to exist*. An operator reading `runtime_gating.py` would
reasonably conclude that omitting a runtime prevents its use and that spend is
bounded. Neither is true.

This also blocks #790: adding `AIFACTORY_RUNTIMES=claude,copilot` to gitops today
is a silent no-op, because `operator_allowlist()` drops tokens that are not in
`KNOWN_RUNTIMES` and `copilot` is not among them
(`runtime_gating.py:42-52`) — while `copilot:<model>` already routes to
`CopilotAgenticProvider` (`providers/factory.py:62`, aliases `:132-134`) with no
gate at all. The allowlist is simultaneously unable to permit Copilot and unable
to forbid it.

## Proposed outcome

Selecting a runtime goes through the operator allowlist on every path that
actually runs an agent, so that:

- A runtime the operator did not enable cannot be selected, by model string or by
  contract, and the refusal says so.
- A runtime the operator *did* enable can be selected — including `copilot`,
  which is what closes #790.
- A signed contract's `execution.runtime` reaches the code that decides, rather
  than being dropped in translation.
- The allowlist reaches the build Job, where the coder actually runs.
- `claude` remains always-enabled and the default, so existing tasks that name no
  runtime are unaffected.

Observable: a task naming a disabled runtime fails with a clear message rather
than running; a task naming an enabled one runs; and with `AIFACTORY_RUNTIMES`
unset, today's behaviour for `claude` is unchanged.

If the conclusion is instead that the gate should not exist, the acceptable
alternative outcome is that `runtime_gating.py` and `get_runtime_provider()` are
**deleted**, so nothing in the tree implies a control that is not there. What is
not acceptable is leaving it as decoration.

## Affected users and systems

- `apps/backend/core/runtime_gating.py`, `apps/backend/providers/factory.py`
- `apps/backend/agents/coder.py`, `agents/planner.py`, the QA paths in
  `agents/qa/loop.py` — the call sites that currently bypass it
- `apps/backend/trusted_plan.py` — the dropped `execution.runtime`
- `apps/web-server/server/services/build_backend.py` — the passthrough
- `factory-gitops apps/aifactory/manifests` — where `AIFACTORY_RUNTIMES` is set
- Anyone using `codex:`, `ollama:`, `antigravity:` model strings today: they are
  ungated now and would become gated. **This is a behaviour change for existing
  users and is the main risk of the whole task.**

## Constraints

- **Must not break existing tasks.** Runtimes in use today must keep working;
  the allowlist has to be populated to match reality before it is enforced, not
  after. Enforcement that starts by refusing current usage is a worse outage than
  the gap it closes.
- `claude` stays always-enabled regardless of the allowlist, as the module
  already documents.
- Fix it once, in the shared selection path, rather than adding a check to
  `coder.py`, `planner.py` and each QA path separately — the duplication is how
  the three call sites drifted out of the gate in the first place.
- The refusal must be legible: which runtime, which are enabled, and where to
  change it.
- Spend-multiplying runtimes must keep needing to be named explicitly; a broad
  token must not enable them.
- No change to what `claude`-only deployments do, which is the current fleet.

## Open questions

1. **Enforce, or delete?** I recommend enforcing: the alternative leaves spend
   unbounded on a multi-tenant control plane, and the module's own reasoning
   about speed-up runtimes is sound. But deleting is a legitimate answer and is
   less work, and I would rather that be a decision than a default.
2. **What must the allowlist contain on day one** so nothing in flight breaks?
   I will determine actual usage from the deployed config and recent tasks before
   the spec proposes a value, rather than guessing.
3. **Should an unknown token be an error rather than silently dropped?**
   `operator_allowlist()` ignoring what it does not recognise is exactly why the
   #790 config line would have been a no-op nobody noticed. Failing loudly on an
   unrecognised token seems right, but it turns a typo in gitops into a failed
   rollout.
