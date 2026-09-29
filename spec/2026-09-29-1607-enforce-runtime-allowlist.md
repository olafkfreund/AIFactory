---
status: draft
issue: 1607
intent: intent/2026-09-29-1607-enforce-runtime-allowlist.md
---

# Spec: one vocabulary, one chokepoint, enforced — and populated before it is

## Corrections to the intent

Three of the intent's facts were understated. None changes the goal; all change
the design.

1. **Eight live call sites, not four.** Beyond `coder.py:456-482`,
   `planner.py:102-122` and `qa/loop.py:231-248,:531-561`, the same
   `infer_provider_from_model → get_provider` shape appears at
   `qa/correction.py:184`, `spec/pipeline/agent_runner.py:162`,
   `agents/parallel_integration.py:456`, and a *second* factory entry point
   `get_qa_llm_provider` (`qa/loop.py:96,106,115,137,141`). This strengthens the
   intent's "fix it once" constraint.

2. **The allowlist is not entirely dead.** `operator_allowlist()` has a real
   caller — `core/provider_failover.py:94` (`enabled_failover_chain`) — reachable
   only via `next_provider(..., gated=True)`. The one production caller,
   `qa/loop.py:628`, omits the keyword and so passes `False`. The plumbing exists
   and is switched off.

3. **`KNOWN_RUNTIMES` and the factory's vocabulary have already drifted**, which
   is the finding that shapes this spec. Verified by set comparison:

   | source | tokens |
   | ------ | ------ |
   | factory canonicals (`providers/factory.py:58-99`) | `antigravity claude codex copilot github-models ollama ollama-cloud openai-compatible opencode` |
   | `KNOWN_RUNTIMES` (`runtime_gating.py:42-52`) | `antigravity claude claude-subagents codex dynamic-workflow ollama ollama-cloud` |

   **`copilot`, `github-models`, `openai-compatible` and `opencode` are producible
   by the live path and absent from the gate's vocabulary.** Because
   `operator_allowlist()` drops unrecognised tokens, enforcing today would make
   those four permanently unreachable — *no value an operator could set would
   re-enable them*. That is worse than the present gap.

   `gemini` is the same trap in miniature: a valid alias for `antigravity`
   (`factory.py` alias table) and absent from `KNOWN_RUNTIMES`.

## Design

### 1. One vocabulary, derived — not a second hand-maintained set

The two lists drifting is the root cause, not an accident to be corrected once.
`KNOWN_RUNTIMES` stops being an independently maintained literal and is derived
from the factory's registries and alias table, so a provider that exists is a
runtime the gate can reason about, by construction.

`claude-subagents` and `dynamic-workflow` have no factory entry — both map to
`"claude"` via `_RUNTIME_TO_PROVIDER` (`factory.py:245-257`) — so they are added
to the derived set explicitly, keeping RFC-0014 §6's rule that a broad token does
not enable a spend-multiplying runtime.

Tokens are normalised through the same alias table `get_provider` uses, so
`AIFACTORY_RUNTIMES=gemini` means antigravity rather than nothing.

### 2. One chokepoint

All eight sites share the shape `infer_provider_from_model(model)` → compare to
`"claude"` → `create_client(...)` or `get_provider(...)`.
`infer_provider_from_model` (`phase_config.py:717`) is the wrong place: callers
test its result before constructing anything, so a raise there fires on the
claude path too.

Both public factory entry points resolve the alias and then call
`_apply_endpoint_defaults` (`providers/factory.py:174`) with the canonical in
hand — `get_provider` at `:305` (after `_resolve_canonical`, `:321`) and
`get_qa_llm_provider` at `:381`. That helper exists *because* those two were
already drifting (#1213, comment `:183-188`).

The gate is called explicitly at `:305` and `:381` rather than hidden inside the
helper, so the enforcement is visible where the decision is made. **No signature
changes:** the gate needs the canonical (already the first argument) and the
environment; it is not phase-parameterised anywhere in RFC-0014.

**Known limit, scoped out deliberately.** `claude` never reaches this point —
every call site short-circuits to `create_client`. So `claude-subagents` and
`dynamic-workflow`, both mapping to `"claude"`, are *not* enforceable at this
seam. Gating the two MANUAL_ENABLE_ONLY runtimes is a separate seam and is out of
scope; the spec says so rather than implying coverage it does not have.

### 3. Populate before enforcing

The intent's binding constraint. Evidence that the risk is real but bounded:

- **History is clean.** 479 `task_metadata.json`/`requirements.json` on the live
  PVC: `haiku` ×116, `claude-sonnet-4-5-20250929` ×31, `sonnet` ×24, `opus` ×7,
  `claude-sonnet-4-5` ×3. Zero non-Claude. Zero `runtime` fields, ever.
- **Exposure is latent, not historical.** The web UI picker
  (`apps/frontend-web/src/shared/constants/models.ts`) offers `gemini-*`
  (→ antigravity) and `gpt-5.*` (→ codex) beside the Claude models.
- **The sharpest break is automatic.** `_DEFAULT_CHAIN = ("claude", "codex",
  "antigravity")` (`provider_failover.py:31`) runs ungated at `qa/loop.py:628`.
  Enforce without allowlisting codex and antigravity and a stalled Claude QA run
  stops degrading gracefully and starts hard-failing.

Day-one value, on the aifactory deployment:

```
AIFACTORY_RUNTIMES=codex,antigravity,openai-compatible,copilot
```

- `codex`, `antigravity` — in the live picker *and* the default failover chain.
- `openai-compatible` — the only runtime with complete endpoint credentials
  deployed (`OPENAI_COMPATIBLE_BASE_URL=https://ollama.com`).
- `copilot` — #790 (below).
- Omitted: `ollama`, `ollama-cloud`, `opencode` — no `OLLAMA_BASE_URL` on
  aifactory and no `ollama`/`opencode` binary in the pod, so they are already
  non-functional. Gating them costs nothing and states the truth.
- `claude` is always enabled and is not listed, per the module's existing rule.

`AIFACTORY_RUNTIMES` is added to `_PASSTHROUGH_BUILD_ENV`
(`build_backend.py:256`). Without it, `AIFACTORY_BUILD_BACKEND=kubejob` means
every dispatched build reads an empty allowlist and gates everything to claude
regardless of what the deployment says.

`qa/loop.py:628` passes `gated=True`. `enabled_failover_chain` already does the
right thing; the fix is one keyword. Without it, failover selects a gated runtime
and then takes a raise from the factory — turning graceful degradation into an
exception.

### 4. Unknown tokens raise

`operator_allowlist()` currently drops them, documented at `runtime_gating.py:82`
as "a typo can never *enable* an unintended runtime."

That protects the wrong failure. Once the gate is enforced, the realistic
operator error is not "a typo enables codex" — it is `AIFACTORY_RUNTMIES=codex`
or `AIFACTORY_RUNTIMES=gemini` silently *disabling* what the operator believes
they just enabled, surfacing hours later as a build failure pointing nowhere near
the variable. Silent drop is precisely how this module became dead code.

Safe to raise: nobody sets the variable today (zero hits across the cluster and
gitops), and it is called lazily at provider construction — importers are
`factory.py:45` and `provider_failover.py:26`, neither at module scope. A gitops
typo therefore costs one build, not the pod; liveness and readiness are
unaffected. `RuntimeNotEnabledError` (`runtime_gating.py:118-140`) already has
the right message shape.

### 5. Signed contracts carry `runtime`

`_EXECUTION_TO_METADATA` (`apps/backend/trusted_plan.py:701-724`) gains a
`"runtime"` entry, so a Task Contract v2 `execution.runtime` reaches the executor
instead of being silently discarded.

### 6. #790 closes here

Everything Copilot needs exists except the registry entry. Verified: provider
wired (`factory.py:62,90`, aliases `:132-134`); `infer_provider_from_model`
returns `"copilot"` on the `copilot:` prefix, checked *before* the claude/gpt
rules because Copilot's own backends are named `claude-sonnet-4.5`/`gpt-5`
(`phase_config.py:771-775`); credential live (secret `factory-cli-creds`, key
`copilot-apps.json`, 152 bytes, well-formed GitHub App OAuth credential — keys
only, no values read); CLI pinned in the image (`Dockerfile:316-346`, with a
`test -x` build assertion) and present in the pod (`GitHub Copilot CLI 1.0.88`).

So #790 reduces to: `copilot` in the derived vocabulary, `copilot` in
`AIFACTORY_RUNTIMES`. No provider, credential or image work.

## Alternatives rejected

**Delete `runtime_gating.py` instead.** The intent's stated alternative. Rejected:
spend stays unbounded on a control plane where `StartTaskRequest.model` is an
unrestricted string on a task-member endpoint, and the module's reasoning about
spend-multiplying runtimes is sound. Enforcing is more work and the right work.

**Gate inside `infer_provider_from_model`.** Fires on the claude path, which every
call site takes today. Would refuse the fleet's only real workload.

**Gate at each of the eight call sites.** The duplication that let three of them
drift out of the gate. Explicitly excluded by the intent.

**Hide the gate inside `_apply_endpoint_defaults`.** Fewer lines, but it buries an
authorisation decision in a helper named for endpoint defaults. Enforcement
should be legible at the point of decision.

**Keep `KNOWN_RUNTIMES` as a literal and just add the four missing tokens.**
Fixes today's drift and guarantees tomorrow's: the two lists have already drifted
once, silently, and nothing would stop it recurring.

**Keep silently dropping unknown tokens.** See §4.

## Risks

- **Enforcement refuses something legitimate.** The failure that matters.
  Mitigated by populating from measured usage, by deriving the vocabulary so no
  live provider is unrepresentable, and by `claude` remaining always-enabled —
  which is 100% of observed history.
- **QA failover breaks.** The sharpest concrete case; addressed by allowlisting
  codex and antigravity and by `gated=True` at `qa/loop.py:628`. Needs a test.
- **The build Job reads an empty allowlist.** Addressed by the passthrough;
  without it enforcement silently gates every dispatched build to claude, which
  would look exactly like a working system until someone selected a runtime.
- **A raise on an unknown token fails a build.** Intended, and cheaper than the
  silent disable it replaces. One build, not the pod.
- **`claude-subagents`/`dynamic-workflow` remain ungated** at this seam. Stated,
  not hidden; separate issue.
- **Deriving the vocabulary couples gating to the registries.** A provider added
  without thought becomes gate-visible automatically. Preferable to the current
  coupling, which is "two lists that must be edited together and were not".

## Verification

1. **Vocabulary parity, as a test.** Every factory canonical is in the derived
   set, and the derived set adds only the two claude-mapped runtimes. This is the
   regression test for the drift that caused the issue — it fails on `dev` today.
2. **Alias normalisation:** `AIFACTORY_RUNTIMES=gemini` enables `antigravity`.
3. **Enforcement:** with the allowlist unset, a `codex:`/`copilot:` model string
   raises `RuntimeNotEnabledError` naming the runtime and the enabled set; with
   the day-one value set, both construct a provider.
4. **`claude` is unaffected** with the allowlist unset, set, or empty — the
   regression net for the whole fleet's current workload.
5. **Unknown token raises**, and the message names the variable.
6. **Failover:** `qa/loop.py` passes `gated=True`, and a chain containing a
   disabled runtime skips it rather than raising.
7. **Passthrough:** `AIFACTORY_RUNTIMES` appears in the dispatched Job's env.
8. **Contract:** an `execution.runtime` survives `execution_profile_to_metadata`.
9. **Copilot end to end:** `copilot:<model>` resolves to `CopilotAgenticProvider`
   when allowlisted and raises when not.
10. **Full suite green; cq-ratchet "0 regressed" on both halves.**
11. **Not verified here:** that a Copilot build actually completes against the
    seeded credential. That needs a paid run and is #790's acceptance, not this
    spec's.
