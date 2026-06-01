# Design — Claude-on-LiteLLM enforcement wrapper (Epic #35 v1.2 / #207)

> Locked from super-brainstorm 2026-05-29 + reviewer audit. Closes the
> v1.1 enforcement gap left open by #38 (Claude calls bypass per-tenant
> budget / allowlist / audit). Implementation in 3 PRs after sign-off.

## Why we're doing this

#38 shipped LiteLLM as the gateway for OpenAI / Codex / Gemini /
Ollama: per-tenant budget enforcement, per-tenant model allowlist,
PII-redacted prompt+response audit (#38 PR-2b — `llm_audit_hook.py` +
`llm_pii_redactor.py`), and Prometheus per-tenant cost dashboards.

**Claude calls bypass all of that.** The reasons are documented in the
v1.1 design doc (`docs/plans/2026-05-28-litellm-gateway-design.md`
section "Scope" + reviewer-finding #1 in its decision-audit table):

- The Claude Agent SDK spawns the `claude` CLI as a subprocess.
- The CLI speaks Anthropic-format `POST /v1/messages`.
- LiteLLM's primary chat endpoint speaks OpenAI-format
  `POST /v1/chat/completions`.
- The two wire formats are not transparently interchangeable, and the
  v1.1 design explicitly deferred the closure ("v1.2 via either
  (a) an in-process Claude-SDK enforcement wrapper in `core/client.py`
  mirroring LiteLLM's enforcement, or (b) a LiteLLM Anthropic-format
  passthrough endpoint if LiteLLM upstream adds one").

The current state, post-#38 PR-2b:

| Concern | OpenAI / Codex / Gemini / Ollama (v1.1) | Claude (v1.1 — today) |
|---------|------------------------------------------|------------------------|
| Per-tenant budget | LiteLLM-side enforced | Undefended |
| Per-tenant model allowlist | LiteLLM-side enforced + `OpenAICompatibleProvider` fail-fast | Undefended |
| Per-call prompt+response audit row | `llm_audit_hook.write_llm_call_audit` per call | None (existing #43 chain audit covers org-level events, not per-call LLM I/O) |
| PII redaction in audit | `PiiRedactor` built-in + operator extras | N/A (no row to redact) |
| Streaming-aware audit (abandoned / failed) | Three action variants (`llm.call`, `.abandoned`, `.failed`) | None |
| Per-tenant cost dashboard | LiteLLM Prometheus + Grafana panel | None |

**Compliance implication.** SOC 2 CC7.2 and ISO 27001 A.12.4.1 require
"log and review user activities, exceptions, faults and information
security events." A compromised tenant using Claude can today exfil
content into a prompt with no per-call audit row and no model-allowlist
enforcement. The existing #43 hash-chain anchor covers the
events the platform DOES write — but it does not write per-LLM-call
events for Claude. v1.2 / #207 closes that.

**Reviewer-finding alignment.** This document discharges
`2026-05-28-litellm-gateway-design.md`'s decision-audit row
"Claude Agent SDK incompatible with `base_url` swap → v1.2 closes"
and the threat-model rows currently marked **"Still undefended in
v1.1 — Claude bypasses gateway"** for budget exhaustion, runaway-loop
cost, and per-tenant model allowlist.

## Out of scope (explicit)

- **Multi-Claude-version routing.** Operator already picks per-org
  Claude variants via `organizations.allowed_models` (PR-2a of #38).
  No new routing logic.
- **Pre-LLM PII scrubbing for Claude prompts.** Covered by the
  separate v1.2 PII-bundle issue (`litellm.audit.scrubBeforeSend`
  mode planned for #210). v1.2 / #207 keeps the same scope discipline
  as #38 PR-2b: redaction applies to the audit row ONLY.
- **Claude tool-use loop audit.** The wrapper produces ONE audit row
  per `client.query()` invocation (the same boundary that
  `OpenAICompatibleProvider._run_request()` uses). Multi-turn tool
  use inside a single agentic session is recorded with the assembled
  final transcript; per-turn tool-use audit (one row per Claude
  `tool_use` / `tool_result` pair) is parking-lot for v1.3.
- **Per-tenant Anthropic API keys.** v1.2 keeps the deployment-wide
  Anthropic key (matches #38 §"Out of scope" → "Per-tenant API keys
  for direct Anthropic billing"). Tenant-billed Anthropic usage is
  v2.0 territory.
- **Bedrock / Vertex Claude variants.** Those already go through
  LiteLLM (#39, OpenAI-format `bedrock/anthropic.*` model strings).
  This document is about the **native Claude Agent SDK path only**.
- **Prompt / response caching.** LiteLLM has a Redis cache layer;
  #38 deferred it to a later v1.2 cache-bundle issue. Not part of #207.
- **Audit-anchor v2 / external pub.** Tracked separately under
  `2026-05-28-audit-anchor-design.md` §"v1.2 external pub".
  v1.2 / #207 reuses the v1.1 chain + classification machinery
  unchanged.
- **Spec-creation and one-off CLI invocations of `create_client()`.**
  The wrapper is opt-in per-call via an explicit `org_id` argument
  (decision §10). System tasks (e.g. `apps/backend/runners/insights_runner.py`,
  `apps/backend/runners/ai_analyzer/claude_client.py`) that pass no
  `org_id` retain v1.1 behaviour — no enforcement, no audit row.
  This avoids a Big-Bang regression and matches the
  `OpenAICompatibleProvider(allowed_models=None)` semantics in
  `apps/backend/providers/openai_compatible.py:_model_allowed()`.

## Architectural options — two reviewed; one picked

### Option A — In-process enforcement wrapper

Wrap `ClaudeSDKClient` in `apps/backend/core/client.py` (and the
sibling `core/simple_client.py`) with budget / allowlist / audit logic
that runs BEFORE invoking the SDK subprocess. State source-of-truth
stays in AIFactory's Postgres (`organizations.allowed_models` +
LiteLLM virtual-key state already provisioned by #38 PR-2b's
`tenant_reconciler`). Enforcement happens in the Python web pod, not
at a network hop.

**Pros.**
- Zero new wire format. The CLI keeps talking to `api.anthropic.com`
  directly; we only add Python guards around its lifecycle. Tool-use,
  thinking, streaming, agentic loops — all unchanged.
- Reuses the existing primitives unchanged: `write_llm_call_audit()`,
  `PiiRedactor`, `_model_allowed()`, `ModelNotAllowedError`.
- No new component to deploy, no new dependency on LiteLLM upstream.
  Works in deployments that don't run a LiteLLM gateway at all
  (currently the supported v1.1 default — `litellm.enabled=false`).
- Token-count + cost capture is exact: the SDK exposes the final
  `Usage` block via `result.usage` (input/output/cache tokens) and
  AIFactory computes the cost from the model's per-token rate.
- Failure isolation is process-local. A broken audit-write doesn't
  cascade through a network hop.

**Cons.**
- We re-implement budget enforcement that LiteLLM already does for
  the other 4 providers. Two enforcement planes (LiteLLM for
  non-Claude, in-process for Claude) instead of one.
- Budget state is split: LiteLLM's Postgres for non-Claude virtual
  keys, AIFactory's Postgres for Claude per-org counters. Cross-LLM
  budget queries ("how much did org A spend total today?") must
  JOIN across both.
- Pre-call budget check is fundamentally race-prone in a multi-replica
  pod world: two replicas can each read "$3 remaining, this call
  costs $2" and both proceed. Mitigation = `SELECT … FOR UPDATE`
  under the org's row OR Redis `INCRBY` with a budget ceiling, both
  of which add latency.

**Complexity:** medium. ~600 LOC in `core/client.py` + a new
`core/enforcement.py` + ~300 LOC of tests. No new schema (reuses
LiteLLM virtual-key budget state as authoritative — see decision §3).

**Risk:** low. No upstream-dependency risk; no wire-format risk; no
new operational surface.

**Security:** equivalent to non-Claude path. Same threat surface as
`OpenAICompatibleProvider`.

### Option B — LiteLLM Anthropic passthrough

Configure LiteLLM with an Anthropic backend; set the Claude SDK's
`ANTHROPIC_BASE_URL` (already passed through to the SDK subprocess by
`apps/backend/core/auth.py` `SDK_ENV_VARS`) to point at LiteLLM.
LiteLLM transparently proxies `/v1/messages` to Anthropic. Audit /
budget / allowlist run at LiteLLM, the same plane that already covers
the other four providers.

**State as of 2026-05-29.** LiteLLM ships
`litellm/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py`
upstream — Anthropic `/v1/messages` passthrough IS implemented today.
Verified via `gh api search/code … repo:BerriAI/litellm
anthropic_passthrough`. However, open upstream bugs as of the same
date:

- **BerriAI/litellm #28562** (open) — passthrough response `id`
  mismatches spend log `request_id`. Breaks our
  `llm_audit_hook.write_llm_call_audit(litellm_request_id=…)` cross-
  reference contract.
- **BerriAI/litellm #28228** (open) — `/v1/messages` passthrough
  cost tracking ignores router pricing. Breaks
  `details_json.cost_usd` for the Claude path.
- **BerriAI/litellm #26749** (open) — `server_tool_use` parsed as
  dict instead of typed object in passthrough usage block. Breaks
  Claude's web-search and computer-use tool accounting.
- **BerriAI/litellm #27512** (open) — passthrough retry drops the
  `thinking` content while keeping `clear_thinking_*` headers.
  Breaks Opus 4.7 extended-thinking output on retry.
- **BerriAI/litellm #29187** (open, Bedrock variant of same issue) —
  upstream errors hang then surface as "Internal server error" with
  the real cause buried. Operationally noisy for Claude Code-style
  agentic loops that retry aggressively.

**Pros (if the bugs were closed).**
- Single enforcement plane for ALL providers. One Grafana dashboard,
  one budget table, one set of admin-API operations.
- Operators who already run LiteLLM for the other providers add
  Claude with zero new code on the AIFactory side (just an
  `ANTHROPIC_BASE_URL` env var per pod).

**Cons (today).**
- The four upstream bugs hit AIFactory's exact use case (agentic
  loops with tool-use + thinking + retries + cost chargeback). Each
  one is a documented v1.2-blocker for the audit contract.
- Adds a hard dependency on `litellm.enabled=true`. Deployments that
  use Claude only and never wanted LiteLLM now need it — that's a
  Helm chart breaking change for the install-Claude-only path.
- The CLI subprocess + SDK message-shape interaction with LiteLLM's
  passthrough is poorly tested upstream for Claude Code-style
  agentic flows (vs. the more common Anthropic Python SDK direct
  call). Reviewer flagged this in v1.1 design and the upstream issue
  tracker confirms it.
- Operationally, when the LiteLLM gateway pod is restarting, ALL
  Claude calls fail — currently they would only fail for non-Claude
  providers. Larger blast radius from a single component.

**Complexity:** low for the happy path (env var + Helm wire-up).
High for the bug-workaround path (we'd need to monkey-patch usage
parsing + maintain a fork-or-pin until each upstream bug closes).

**Risk:** high — four open upstream bugs in our exact code path.

**Security:** equivalent or slightly worse (single point of failure
for all providers; same enforcement strength when working).

### Decision — pick Option A

**Pick Option A (in-process enforcement wrapper).**

**Reasoning.**
1. **The four open LiteLLM bugs (#28562, #28228, #26749, #27512)
   land on exactly AIFactory's contract:** `litellm_request_id`
   cross-reference, cost accuracy, tool-use accounting, and Opus 4.7
   thinking. The audit row that drives SOC 2 evidence is corrupted
   on each of these paths. Option B works "in principle" — it
   doesn't work for AIFactory's agentic-Claude path today.
2. **Option A reuses 100% of the #38 PR-2b primitives** without
   modification. `write_llm_call_audit`, `PiiRedactor`,
   `ModelNotAllowedError`, and the `["*"]` backward-compat
   convention all transfer directly.
3. **Standalone-Claude deployments stay supported.** The
   `litellm.enabled=false` path remains valid; the wrapper enforces
   in-process without requiring a gateway hop.
4. **Once the upstream bugs close (likely 2026 mid-year), v1.3
   can re-evaluate Option B** as a unification refactor. The
   wrapper's interface is intentionally narrow (decision §1) so
   that swap remains low-cost.

We accept the "two enforcement planes" complexity in exchange for
correctness today.

## Locked decisions

### 1. Wrapper home — new `apps/backend/core/enforcement.py`, called from `create_client()`

A new module `apps/backend/core/enforcement.py` (~250 LOC) owns the
Claude-specific enforcement primitives. Public API:

```python
# apps/backend/core/enforcement.py

class ClaudeEnforcementContext:
    """Per-call context: org_id + user_id + allowlist + budget snapshot.

    Constructed by create_client() when org_id is supplied. None-valued
    instance (returned by `noop()`) bypasses enforcement — matches the
    OpenAICompatibleProvider(allowed_models=None) semantics.
    """

    def __init__(
        self,
        org_id: str,
        user_id: str | None,
        model: str,
        allowed_models: list[str],
        # Inject for tests; production wires from LiteLLMAdminClient.
        budget_provider: BudgetProvider | None = None,
    ) -> None: ...

    def enforce_pre_call(self) -> None:
        """Raise ModelNotAllowedError or BudgetExceededError. Fail-fast."""
        ...

    async def record_post_call(
        self,
        usage: ClaudeUsageSnapshot,
        prompt_text: str,
        response_text: str,
        action: str,           # llm.call | .abandoned | .failed
        error: str | None = None,
    ) -> None:
        """Write audit row + decrement budget. Failure-safe."""
        ...

    @classmethod
    def noop(cls) -> "ClaudeEnforcementContext":
        """Bypass enforcement (system tasks, CLI mode, unspecified org)."""
        ...
```

`core/client.py:create_client()` gains two optional kwargs:

```python
def create_client(
    project_dir: Path,
    spec_dir: Path,
    model: str,
    agent_type: str = "coder",
    ...,
    # v1.2 / #207 — opt-in per-call enforcement plane.
    org_id: str | None = None,
    user_id: str | None = None,
) -> ClaudeSDKClient:
    ...
    enforcement = (
        ClaudeEnforcementContext(org_id=org_id, user_id=user_id, ...)
        if org_id is not None
        else ClaudeEnforcementContext.noop()
    )
    enforcement.enforce_pre_call()  # raises ModelNotAllowedError before SDK spawn
    return _EnforcedClaudeSDKClient(
        underlying=ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs)),
        enforcement=enforcement,
    )
```

`_EnforcedClaudeSDKClient` (private, in `core/enforcement.py`) is a
thin adapter that intercepts `__aenter__` / `__aexit__` / `query` /
`receive_response` to capture prompt+response+usage and call
`enforcement.record_post_call()` in `__aexit__` (success) or in the
`asyncio.CancelledError` / generic exception branches (abandoned /
failed). Same three-variant audit shape as
`OpenAICompatibleProvider._run_request()`
(`apps/backend/providers/openai_compatible.py:227`).

**Why a new module, not extending `core/client.py`?**
`core/client.py` is already 992 lines and threads tool / MCP / cache /
fast-mode / thinking-level / remote-control concerns. Adding the
enforcement plane in-line would push it past 1200 lines and obscure
the call site. A separate module also makes the v1.3 Option-B
re-evaluation a single-file change.

**Why intercept at the wrapper, not subclass `ClaudeSDKClient`?**
The SDK's class is `final`-ish — it's the public API surface and
extending it ties us to its internal layout. Composition via adapter
is robust to SDK minor-version bumps.

### 2. Allowlist match semantics — reuse `_model_allowed()` from `openai_compatible.py`

The existing helper at
`apps/backend/providers/openai_compatible.py:_model_allowed()`
(lines 110-122) already handles:
- `None` (caller opted out) → True
- `["*"]` (schema default) → True
- `fnmatch.fnmatchcase` per pattern (so `"claude-*"` matches
  `"claude-opus-4-7"`)

The wrapper imports this directly. Zero duplication. `ModelNotAllowedError`
from the same module is reused — operators get the same error class
across Claude and non-Claude paths, the same error message format, the
same `.org_id` attribute for log grepping.

**Why fnmatch, not regex?** Already locked in #38 PR-2b. Operators
write `claude-*` and it matches `claude-opus-4-7`, `claude-sonnet-4-5`,
etc. Regex would be more powerful but more dangerous (ReDoS, escaping
confusion). The fnmatch convention is documented in `models.py:163`
`allowed_models` column comment.

### 3. Budget storage — read from LiteLLM virtual-key state as authoritative

`#38 PR-2b` already provisions a LiteLLM virtual key per org via
`LiteLLMAdminClient.create_virtual_key()` (`apps/web-server/server/services/litellm_admin_client.py:158`).
That key carries `max_budget` (USD/day) + `models` (the same allowlist
the Claude wrapper enforces). The wrapper's budget check reads
**LiteLLM's** state via `LiteLLMAdminClient.get_virtual_key_info()`
(line 250) and treats LiteLLM as the source of truth.

```python
class BudgetProvider:
    """Reads remaining-budget for an org from LiteLLM's admin API.

    Production: wraps LiteLLMAdminClient.get_virtual_key_info() and
    extracts `info.spend` + `info.max_budget` to compute headroom.
    Tests: injected fake returning a fixed remaining_usd float.
    """

    async def remaining_usd(self, org_id: str) -> float | None:
        """None = no budget configured (unrestricted)."""
        ...
```

**Why not a new AIFactory-side ClaudeUsageRecord table?**
- Splits the source of truth (org A's "$500 budget today" lives in
  two places, with eventual-consistency drift the operator has to
  reason about).
- LiteLLM's reconciler already runs (decision §3 of #38 — virtual-key
  drift recovery). Adding a second counter would require a second
  reconciler.
- Cross-LLM chargeback queries ("total org A spend today across all
  models") work natively when LiteLLM is the single ledger.

**Why not auto-create a virtual key for Claude-only orgs that don't
have one yet?** This is the migration path (decision §9). Briefly:
when `org_id` is supplied to `create_client()` and the org has no
virtual key yet, the wrapper calls `tenant_reconciler.reconcile_org`
to ensure one exists. Reconciler is idempotent (#38 PR-2b lifecycle
spec). If `litellm.enabled=false` the wrapper falls back to "no
budget enforcement, audit still writes" — documented in §10.

**Race condition for concurrent calls within a single budget window.**
Same problem the non-Claude path has. LiteLLM's per-key budget
counter is the authority, and it accepts/rejects requests
atomically at request time. For Claude we approximate with a
pre-call read + post-call write: a small over-spend window is
possible but bounded by the per-call cost (cents to single-digit
dollars, never the multi-hundred-dollar runaway the gap is here to
prevent). Documented as a known limitation (see decision §5).

### 4. Audit shape — reuse `write_llm_call_audit` unchanged

The wrapper's `record_post_call()` calls
`apps/web-server/server/services/llm_audit_hook.py:write_llm_call_audit`
with **exactly the same parameters** the non-Claude path passes. The
audit row shape is byte-identical between Claude and non-Claude calls:

- `action`: `llm.call` / `llm.call.abandoned` / `llm.call.failed`
  (the same three-variant taxonomy from #38 PR-2b §5).
- `resource_type`: `"llm"` (literal).
- `resource_id`: the model string (e.g. `claude-opus-4-7`).
- `classification`: `"confidential"` (mandated by
  `llm_audit_hook.py:290`).
- `details_json`:
  - `model`, `input_tokens`, `output_tokens`, `cost_usd`,
    `cost_source: "litellm_estimate"` (yes, even on the Claude path
    — operators ALREADY know v1.1 cost is an estimate and the field
    name preserves query compatibility; the wrapper computes the cost
    from the model's per-token rate, see decision §6).
  - `latency_ms`, `prompt_truncated`, `response_truncated`,
    `litellm_request_id` (always `None` on the Claude path — that's
    OK; the field is documented optional).
  - `provider: "claude_sdk"` added to distinguish path. Documented
    addition; backward-compatible (operators querying for `llm.call`
    rows see the new key but their existing filters keep working).

**Why the existing 4 KB truncation cap is fine for Claude.**
Same scope discipline as #38 PR-2b §5: the audit row stays
bounded; operators wanting full text opt in via
`litellm.audit.fullTextCapture=true` (which lives in the existing
encrypted-rows column path from Epic #26 P2). v1.2 / #207 does NOT
change the truncation rule.

### 5. Pre-call budget enforcement — best-effort with explicit race-window doc

```python
def enforce_pre_call(self) -> None:
    # 1. Allowlist check — synchronous; raises ModelNotAllowedError.
    if not _model_allowed(self.model, self.allowed_models):
        raise ModelNotAllowedError(...)

    # 2. Budget check — best-effort. Failure-safe: when LiteLLM is
    #    unreachable (admin API down) we let the call proceed + log
    #    WARNING. The post-call write will still happen + record the
    #    over-spend if it occurs.
    try:
        remaining = await self.budget_provider.remaining_usd(self.org_id)
    except LiteLLMAdminUnavailableError:
        logger.warning("Budget pre-check skipped (LiteLLM unreachable)")
        return
    if remaining is not None and remaining <= 0:
        raise BudgetExceededError(self.org_id, remaining)
```

**Failure-mode choice — `enforcement.failure_mode`.** Matches #38's
gateway-failure handling. Default = fail-closed: if LiteLLM admin is
DOWN at the budget-check step AND the operator has set
`AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE=closed` (the new env var
mirroring the #38 `LITELLM_AUDIT_FAILURE_MODE`), the wrapper raises
`BudgetCheckUnavailableError` and the agent task fails with an
operator-actionable message. Default = open (consistent with the
non-Claude path's "audit failures don't fail tasks" pattern from #38
PR-2b's `try/except` around the audit write).

**Documented race-window.** Two replicas can each read "$3
remaining, $2 call cost" and both proceed. The over-spend cap is
ONE call's cost per replica per budget window. For a Claude Opus
call at $1-5/call the worst case is "a few dollars over the daily
budget per replica." This is intrinsic to read-then-decide
enforcement; the alternative (Redis `INCRBY`-based reservation
with refund-on-failure) adds latency for a small accuracy
improvement and is parking-lot for v1.3 if a tenant ever sees a
material over-spend.

### 6. Token + cost capture — read from SDK `result.usage`; cost from per-model rate table

The Claude Agent SDK emits a final `ResultMessage` with a `usage`
field containing input_tokens, output_tokens, cache-read /
cache-creation tokens (the SDK's
`ClaudeSDKClient.receive_response()` async generator yields this as
the stream terminator). The wrapper accumulates this via its
`receive_response` interceptor, then:

```python
@dataclass
class ClaudeUsageSnapshot:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    model: str

    def estimated_cost_usd(self) -> float | None:
        rate = _CLAUDE_PRICING.get(self.model)  # dict literal in module
        if rate is None or self.input_tokens is None:
            return None
        return (
            self.input_tokens * rate["input"] +
            (self.output_tokens or 0) * rate["output"] +
            (self.cache_read_tokens or 0) * rate["cache_read"] +
            (self.cache_creation_tokens or 0) * rate["cache_creation"]
        ) / 1_000_000  # rates are per-million-tokens
```

`_CLAUDE_PRICING` is a small static dict in `core/enforcement.py`
keyed by canonical model id (`claude-opus-4-7`, `claude-sonnet-4-5`,
etc.) with per-million-token rates as of pricing date. Documented as
"requires manual bump on model release"; out-of-date rates produce
under-estimates that the audit row's `cost_source` field flags as
approximate (same disclosure pattern as #38 PR-2b §5).

**Why not call LiteLLM for the cost calculation?** LiteLLM has its
own pricing table that ages similarly. We're not going through
LiteLLM on the Claude path; we'd be duplicating its dict in
process. Equivalent accuracy with no extra dependency.

**Streaming-cancellation snapshot.** When `asyncio.CancelledError`
fires mid-stream, the wrapper's `__aexit__` extracts the
partially-accumulated usage (Claude SDK provides per-message usage
deltas during streaming). The audit row uses `action="llm.call.abandoned"`
+ `details_json.truncated=True` — same shape as the non-Claude path.

### 7. PII redaction — reuse `PiiRedactor` unchanged, audit-row-only

The wrapper calls into `write_llm_call_audit`, which already
constructs a `PiiRedactor` per call (see
`apps/web-server/server/services/llm_audit_hook.py:223`). Zero
Claude-specific patterns. The built-in pattern set (SSN, email,
phone) and operator extras (via `LITELLM_AUDIT_EXTRA_PATTERNS`
env var) apply identically.

**Same scope discipline as #38 PR-2b §4.** Redaction is applied to
the audit row ONLY. The Claude API has already seen the unredacted
prompt + has already returned the unredacted response by the time
the wrapper writes the row. This is intrinsic to LLM use; the
v1.2 pre-call scrub mode lives in a separate issue (#210).

### 8. Failure-safe contract — same envelope as #38 PR-2b

Same three-clause pattern locked in #38:

1. **Audit-write failure** → log WARNING, do NOT propagate. The
   underlying Claude call already succeeded; we will not corrupt the
   user's session by failing it post-hoc. `try/except Exception:` at
   the top of `record_post_call`, mirroring
   `llm_audit_hook.py:294-303`.
2. **Pre-call budget-check unavailability** → log WARNING, proceed
   (default `failure_mode=open`). Operators on
   `failure_mode=closed` see `BudgetCheckUnavailableError`. Same
   env-var shape as #38's gateway-failure handling.
3. **Pre-call allowlist rejection** → raises `ModelNotAllowedError`
   BEFORE the SDK subprocess spawns. Fast-fail, matches non-Claude
   `OpenAICompatibleProvider.__init__`. Operators see a clear log
   line; no orphaned subprocess.

### 9. Migration / backward compat — wrapper is opt-in per call site

- Existing `create_client()` call sites that pass NO `org_id` (the
  default) get noop enforcement: no allowlist check, no budget
  check, no audit row. Behaviour byte-identical to v1.1.
- Existing organizations with `allowed_models = ["*"]` (the schema
  default — see `models.py:163`) bypass the allowlist branch even
  when `org_id` is supplied. Behaviour byte-identical to v1.1.
- Web-server call sites (agentic task execution paths in
  `apps/web-server/server/services/agent_service.py`) gain a small
  patch: where the spec/task has an owning `org_id`, pass it through
  to `create_client()`. The patch is mechanical and lands in PR-2.
- CLI / standalone invocations (`apps/backend/spec_runner.py`,
  `apps/backend/run.py`, `apps/backend/runners/insights_runner.py`,
  `apps/backend/runners/ai_analyzer/claude_client.py`) are NOT
  modified in v1.2 / #207. They retain v1.1 behaviour and are
  treated as "trusted operator-local invocations" (the same way the
  non-Claude `OpenAICompatibleProvider` can be constructed with
  `allowed_models=None` for trusted uses).

**Forward-migration path for CLI use.** When the v1.3 unification
refactor lands (either upgraded Option B or a cleaner Option A
front-end), the CLI gets a `--org-id` flag that propagates through.
Not in v1.2 scope.

### 10. Wrapper applies only when `org_id` is passed; LiteLLM gateway is OPTIONAL

The wrapper does NOT require `litellm.enabled=true`. The two
enforcement primitives degrade independently:

| `org_id` | `litellm.enabled` | Allowlist | Budget | Audit |
|----------|-------------------|-----------|--------|-------|
| None | any | skipped | skipped | skipped |
| set | true | enforced | enforced (LiteLLM admin API) | written |
| set | false | enforced (from `Organization.allowed_models`) | skipped (no virtual key to read) + WARNING logged | written |

This is critical: a Claude-only deployment can adopt #207 without
deploying LiteLLM. They get allowlist enforcement + per-call audit
rows (the high-value pieces). Budget enforcement comes when they
opt into LiteLLM later. Same opt-in cadence #38 PR-3 already
established.

## Reviewer-audit pass

Six critical findings + five recommendations. All baked into the
locked decisions above where applicable; where not, called out below.

### Critical finding #1 — Multi-turn tool-use loops fire enforcement once per session, not per turn

`ClaudeSDKClient` agentic loops can run for dozens of turns inside a
single `query()`-to-stream-end cycle. The wrapper hooks at the
`query()` / `receive_response()` boundary, so it sees ONE
prompt-in + ONE assembled-response-out per session. A 50-turn
session with $50 of tool-use cost becomes ONE audit row of $50.

**Resolution.** Accepted as v1.2 scope (see "Out of scope"). The
single-row-per-session model matches what the non-Claude
`OpenAICompatibleProvider` does today (one call to the chat endpoint
= one audit row). Per-turn tool-use audit is parking-lot for v1.3 —
that requires hooking deeper into the SDK's message stream and is a
materially larger surface. Documented as a known v1.2 limitation
in the concept doc.

**Mitigation in the budget direction.** The pre-call budget check
catches "tenant has $0 left, no more sessions." It doesn't catch
"tenant runs a 50-turn session that spends $50 within a single
session." That second case is bounded by the SDK's `max_turns=1000`
ceiling (already set in `core/client.py:914`) + the agentic loop's
natural termination. For a worst-case Opus tool-use loop at ~$1/turn
the in-session over-spend cap is $1000 — still bad, but bounded and
visible in the (single) audit row. Operators wanting tighter caps
can lower `max_turns` per agent_type in `AGENT_CONFIGS`.

### Critical finding #2 — Standalone Claude deployments (no LiteLLM) need allowlist + audit but get no budget

The decision-§10 table above documents this. Two sub-concerns:

**(a) Allowlist source-of-truth when LiteLLM is absent.** Read
directly from `Organization.allowed_models` (the same JSONB column
#38 PR-2a added). The wrapper's `ClaudeEnforcementContext` is
constructed with the allowlist passed in; the call site
(agent_service.py) fetches the org row + threads the list through.

**(b) Operator visibility of the "no budget" state.** When
`org_id` is supplied but no virtual key exists, the wrapper logs
WARNING **once per process** (deduped via `lru_cache`-keyed set):
"Claude budget enforcement skipped for org=<id> — no LiteLLM
virtual key configured. Per-call audit + allowlist remain active.
Enable LiteLLM gateway for per-tenant budget enforcement."
Operators see a clear actionable message.

### Critical finding #3 — Race: budget exceeded mid-session, stream still running

Two replicas, $3 budget remaining, $2 cost per call. Both pass the
pre-check. Both run. The post-call write decrements both. Org is
now $-1 over budget; the next call sees $0 remaining and is
correctly blocked.

**Resolution.** Documented limitation (see decision §5 race-window
para). The over-spend cap is ONE call per replica per budget
window — bounded, small, and self-correcting on the next call.
**Not in scope:** mid-stream cancellation when the budget is hit.
The Claude SDK doesn't expose a clean cancel mechanism that
respects the in-flight tool-use chain; aborting mid-loop leaves
partial state in the project workspace. We let the in-flight call
complete + write the over-spend audit row + block subsequent calls.
The threat-model table reflects this — "runaway agent costs $1k
overnight" goes from undefended to "bounded by per-call cost +
audit-visible," not "perfectly prevented."

### Critical finding #4 — Per-tenant vs deployment-wide Claude API key

v1.1 uses ONE deployment-wide Claude OAuth token
(`CLAUDE_CODE_OAUTH_TOKEN`). All tenants' calls bill to the same
Anthropic account. v1.2 / #207 does NOT change that. Therefore:
- The wrapper's per-tenant budget enforcement protects the
  deployment from one tenant exhausting the SHARED Anthropic budget.
- It does NOT enable per-tenant Anthropic billing (the operator
  still gets one invoice from Anthropic).
- It DOES enable per-tenant CHARGEBACK from the audit row's
  `details_json.cost_usd` field (operators run their own billing
  query against audit rows).

**Why explicit.** A reviewer reading the design might assume "per-
tenant budget" means "per-tenant Anthropic account." It does not.
Same scope as #38 PR-2b ("Per-tenant API keys for direct Anthropic
billing" is explicit out-of-scope). Documented in the concept doc.

### Critical finding #5 — Audit-hook performance overhead per Claude call

Every Claude session now does:
- 1 LiteLLM admin API call (budget pre-check; ~50-200ms typical).
- 1 `Organization` table read (allowlist load; <5ms typical, indexed
  by `org_id` already).
- 1 audit row write (post-call; <20ms typical including hash-chain).
- 1 KMS unwrap per LiteLLM admin call (cached by
  `LiteLLMAdminClient._unwrap_master_key`'s per-request lifetime;
  ~10ms unwrap cost amortized to ~0 over a session).

Total added wall-clock per session: ~100-300ms. Claude Opus sessions
are typically 10-60s; the overhead is 1-3%. Acceptable. Documented
in the concept doc.

**Optimization for hot-path operators.** The LiteLLM admin client
gains an optional in-process LRU cache (~5-second TTL) for budget
reads. v1.2 ships without it (premature optimization); revisit if
the 100-300ms surfaces as a real problem.

### Critical finding #6 — `core/simple_client.py` also creates ClaudeSDKClient

`apps/backend/core/simple_client.py` (line 27) creates a
`ClaudeSDKClient` for single-turn operations (e.g. some web-server
helpers). It bypasses `core/client.py:create_client()` entirely. If
left unwrapped, simple_client provides an enforcement bypass.

**Resolution.** PR-2 patches `create_simple_client()` to ALSO accept
`org_id` and route through `_EnforcedClaudeSDKClient` when supplied.
Same opt-in semantics: no `org_id` = no enforcement. Test added:
`test_simple_client_org_id_threads_enforcement`.

### Recommendations (5)

1. **Per-model cost table maintenance.** Add a `tests/test_claude_pricing.py`
   that asserts every Claude model name actually used in production
   (parsed from `phase_config.py` model lists) has a `_CLAUDE_PRICING`
   entry. Catches "added a new model, forgot to bump the dict."
2. **Audit-row `provider` field.** Add `provider: "claude_sdk"` to
   `details_json` so dashboards can split Claude vs non-Claude spend
   cleanly. Backward-compatible (new key; old queries unaffected).
3. **Wrapper telemetry.** Emit Prometheus counters
   `aifactory_claude_enforcement_calls_total{result="allowed|blocked_allowlist|blocked_budget|audit_failed"}`
   so operators can dashboard the enforcement state. Sits next to the
   existing LiteLLM panel set.
4. **Doc the v1.3 swap path.** Add an explicit "When to revisit
   Option B" section in the concept doc, listing the four upstream
   LiteLLM bugs by number + recommending operators watch for their
   close as the trigger to evaluate the unification refactor.
5. **System-task convention.** Document in `CLAUDE.md` that any new
   call to `create_client()` from web-server code SHOULD pass `org_id`
   when the calling context has one. CLI / runners stay exempt.

## Implementation plan — 3 PRs

### PR-1 — `core/enforcement.py` + `_CLAUDE_PRICING` + unit tests

Schema: none. No DB migration.

- `apps/backend/core/enforcement.py` — new module:
  - `ClaudeEnforcementContext` class.
  - `ClaudeUsageSnapshot` dataclass.
  - `BudgetProvider` protocol + `LiteLLMBudgetProvider` concrete impl.
  - `BudgetExceededError`, `BudgetCheckUnavailableError` typed
    errors. (Reuses `ModelNotAllowedError` from `providers.openai_compatible`.)
  - `_CLAUDE_PRICING` static dict.
  - `_EnforcedClaudeSDKClient` adapter (intercepts query / receive /
    aexit; calls `record_post_call`).
- `tests/test_claude_enforcement.py`:
  - Allowlist match / mismatch (reuses `_model_allowed` parameterized
    cases from `tests/test_openai_compatible.py` for symmetry).
  - Budget pre-check with mocked `BudgetProvider`.
  - Three audit-action variants (success, abandoned, failed) with
    mocked `write_llm_call_audit`.
  - `noop()` bypass.
  - `failure_mode=open` vs `closed` env behaviour.
  - `_CLAUDE_PRICING` completeness for every model in
    `phase_config.py`.
- `tests/test_simple_client_org_id.py` (single test for the
  simple-client opt-in pre-wire).

No `create_client()` wire-in yet. PR-1 ships the enforcement core in
isolation so PR-2's web-server changes can build against a stable
interface. Same shape as #38 PR-1 (env-redirect + types, no wire-in).

### PR-2 — `create_client()` + `create_simple_client()` wire-in + agent_service threading + audit-shape `provider` field

**Merge constraint:** blocked on #38 PR-2b landing on `dev` (this PR
depends on `write_llm_call_audit` + `LiteLLMAdminClient`). PR
description must call this out so reviewers don't green-light early.

- `apps/backend/core/client.py:create_client()` gains `org_id` +
  `user_id` kwargs; wraps the returned client in
  `_EnforcedClaudeSDKClient` when `org_id` is supplied.
- `apps/backend/core/simple_client.py:create_simple_client()` gets
  the same treatment.
- `apps/web-server/server/services/agent_service.py` threads
  `org_id` (from `Task.project.org_id`) + `user_id` (from
  `Task.created_by`) into every `create_client()` call site.
  Mechanical patch — search-and-replace across ~6 call sites.
- `apps/web-server/server/services/llm_audit_hook.py` —
  `write_llm_call_audit()` gets an optional `provider` kwarg that
  lands in `details_json["provider"]`. Default `None` for backward
  compat (existing non-Claude callers unaffected).
- `apps/backend/providers/openai_compatible.py:_run_request()` —
  passes `provider="openai_compat"` (tiny change for dashboard
  cleanliness; doesn't alter behaviour).
- Per-call telemetry counters (`aifactory_claude_enforcement_calls_total`)
  registered via the existing Prometheus client.
- Integration test against in-process SQLite + mocked
  `LiteLLMAdminClient` mirroring PR-2b's test pattern.
- Documented WARNING-once-per-org log line for the "LiteLLM-absent"
  case from reviewer finding #2(b).

### PR-3 — Helm wiring + concept doc + Grafana panel + CHANGELOG

- `charts/aifactory/values.yaml` — `claudeEnforcement:` block:
  ```yaml
  claudeEnforcement:
    enabled: true                    # default ON for new installs
    failureMode: open                # open | closed
    # Operator notes — see concepts/claude-enforcement.md
  ```
  Helm template translates these into the wrapper's env vars
  (`AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED`,
  `AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE`). When `enabled=false`,
  `create_client()` skips the wrap (same as `org_id=None`) — operator
  escape hatch for emergency rollback without code changes.
- `charts/aifactory/dashboards/litellm.json` — add a fourth panel:
  "Claude enforcement: allow / block / audit-fail rates per org."
- `docs/docs/concepts/claude-enforcement.md` — new user-facing
  concept doc, paired with the existing
  `docs/docs/concepts/litellm-gateway.md`. Cross-links both ways +
  walks the operator through the three deployment modes
  (Claude-only, LiteLLM-only, both) and the corresponding enforcement
  matrix from decision §10.
- `docs/sidebars.ts` — add `concepts/claude-enforcement` under the
  existing Concepts category (sidebars.ts:36-38 — adjacent to
  `concepts/litellm-gateway`). Plans docs are NOT sidebared; only the
  concept doc is.
- CHANGELOG.md v1.2 entry strikes the v1.1 limitation "Claude calls
  bypass per-tenant LLM enforcement" from the post-v1.1 known-issues
  list.
- End-to-end test in the existing `helm` CI job: install chart with
  `claudeEnforcement.enabled=true`, create an org with a narrow
  allowlist, run a Claude task with a non-allowed model, assert
  `ModelNotAllowedError` surfaces + audit row written.

## Failure-safe contract

Same envelope as #38 / #41 / #42 / #43:

- Every audit-write wraps in `try/except`. A failed audit write
  logs WARNING but does NOT block the Claude call (the call already
  succeeded; the audit is a separate post-write concern).
- A failed PII-redaction pass logs WARNING + writes the unredacted
  text. Inherits #38 PR-2b's `_PassthroughRedactor` fallback.
- A failed LiteLLM admin API call during budget pre-check:
  `failure_mode=open` (default) logs WARNING + proceeds;
  `failure_mode=closed` raises `BudgetCheckUnavailableError`.
- Operator opt-out via `claudeEnforcement.enabled=false`
  short-circuits the wrap entirely. Emergency rollback path.
- A bug in `_EnforcedClaudeSDKClient` MUST NOT crash the agent pod:
  the adapter's outer `try/except Exception:` (in `__aexit__`)
  catches any unexpected error, logs ERROR with stack, and re-raises
  the underlying Claude call's result/error unmodified.

## Threat model

| Threat | Pre-#207 (v1.1 state) | Post-#207 (v1.2) |
|--------|-----------------------|-------------------|
| Org A's compromised agent exfils data via a Claude prompt | Partial (existing chain audit captures session-level events; per-call prompt/response NOT recorded) | Defended (per-call audit row with PII-redacted prompt + response, classification=confidential, hash-chained) |
| Org A exhausts deployment-wide Claude budget | **Undefended** | Defended (per-tenant budget enforced via LiteLLM virtual-key state) when `litellm.enabled=true`; allowlist still enforced when `litellm.enabled=false` |
| Org A's runaway loop costs $10k overnight via Claude | **Undefended** | Defended (pre-call budget check blocks subsequent calls after the budget is hit; the in-flight call's worst case is bounded by per-call cost, see decision §5) |
| Org A uses `claude-opus` when paid only for `claude-sonnet` | **Undefended** | Defended (fail-fast `ModelNotAllowedError` before SDK subprocess spawns) |
| Org A's Claude session runs 50 turns of tool-use, each $1, total $50 | **Undefended; no audit** | Detected (single audit row records the $50; pre-call budget check blocks the NEXT session). Per-turn enforcement is parking-lot v1.3 (reviewer finding #1) |
| Multi-replica race: two replicas approve calls totaling > budget | N/A | Bounded over-spend (≤ one call cost per replica per budget window); documented limitation, self-corrects on next call (reviewer finding #3) |
| `core/simple_client.py` provides an enforcement bypass | Yes (bypasses `create_client()`) | Defended (simple_client also accepts `org_id` per PR-2, reviewer finding #6) |
| CLI / spec_runner invocations bypass enforcement | Yes | **Same — explicit out-of-scope.** CLI invocations are operator-local + trusted; v1.3 may add `--org-id` flag |
| LiteLLM admin API DOWN at budget-check time | N/A (no budget enforcement) | Default fail-open + WARNING; operator opt-in `failure_mode=closed` for strict deployments |
| Audit-write fails (DB issue) on a Claude call | N/A | WARNING logged; call succeeds; matches non-Claude path (intrinsic failure-safe trade-off) |
| Per-tenant Anthropic billing | Out of scope | **Still out of scope** — v1.2 enables chargeback queries via audit rows; Anthropic invoice remains deployment-wide |

## Open questions

These need resolution at review time (most likely at PR-2 review).

1. **Cache-token cost weighting.** Anthropic's prompt-cache pricing
   is roughly 0.1x for cache-read tokens and 1.25x for cache-write
   tokens vs base input rate. The `_CLAUDE_PRICING` dict needs the
   four-way breakdown locked per model (decision §6 sketches it but
   the per-model multipliers need verification against current
   Anthropic published rates at PR-1 time).
2. **`failure_mode` default for Helm.** Locked at `open` above (matches
   #38). Compliance team may prefer `closed` for strict deployments;
   answer goes into PR-3's values.yaml comment.
3. **Per-turn tool-use audit.** Confirmed out-of-scope for v1.2 (see
   reviewer finding #1). Open question for v1.3: is the right
   approach to (a) hook deeper into the SDK's message stream, or
   (b) implement a tool-use approval flow that records each
   tool-use approval as a separate audit row? Decision deferred.
4. **CLI `--org-id` flag.** Should v1.2 ship a stub
   `--org-id` flag on `spec_runner.py` / `run.py` even if it just
   warns "not yet enforced"? Default answer: no (scope creep).
   Revisit if operators request it during the v1.2 RC window.
5. **`organizations.allowed_models` source-of-truth duality.** Today
   the value lives in AIFactory's DB AND gets pushed into LiteLLM
   virtual-key state. The Claude wrapper reads from AIFactory's DB
   (allowlist) but reads budget from LiteLLM. Should the allowlist
   ALSO be read from LiteLLM for consistency? Argument FOR: single
   source of truth. Argument AGAINST: LiteLLM admin call latency
   on every pre-check vs. a fast indexed `Organization` lookup.
   Default answer: keep the split (latency wins); revisit if drift
   becomes a real operational concern.

## Decision audit summary

10 of 10 brainstorm decisions taken on recommended options.
Reviewer audit pass surfaced 6 critical findings + 5
recommendations; all baked in above:

| Finding | Resolution |
|---------|------------|
| **LiteLLM Anthropic passthrough has 4 open upstream bugs in our exact code path** | Option B rejected; Option A picked (see Architectural options §"Decision"); v1.3 may re-evaluate when bugs close |
| **Wrapper home + intercept point** | Locked: new `apps/backend/core/enforcement.py`; composition adapter, not subclass (§1) |
| **Allowlist match reuse** | Locked: import `_model_allowed` from `providers.openai_compatible`; reuse `ModelNotAllowedError` (§2) |
| **Budget storage source-of-truth** | Locked: LiteLLM virtual-key state as authoritative; no new AIFactory table; cross-LLM chargeback works natively (§3) |
| **Audit-row shape** | Locked: byte-identical to non-Claude path; new `provider="claude_sdk"` field is backward-compat addition (§4) |
| **Pre-call enforcement race window** | Documented limitation; bounded over-spend; `failure_mode` env var mirrors #38 (§5) |
| **Token + cost capture** | Locked: SDK `result.usage` + in-process `_CLAUDE_PRICING` dict; `cost_source="litellm_estimate"` preserved for query compat (§6) |
| **PII redaction reuse** | Locked: zero Claude-specific patterns; audit-row-only scope same as #38 PR-2b (§7) |
| **Failure-safe envelope** | Locked: same three-clause shape as #38; broken wrapper does NOT crash agent pod (§8) |
| **Backward compat / migration** | Locked: opt-in per call site via `org_id`; CLI / runners unchanged; mechanical agent_service patch (§9) |
| **LiteLLM-optional deployment mode** | Locked: wrapper enforces allowlist + audit even without LiteLLM; budget enforcement is the only LiteLLM-dependent piece (§10) |
| Multi-turn tool-use loop = one audit row | Accepted v1.2 scope (reviewer finding #1); per-turn audit parking-lot v1.3 |
| Standalone Claude deployments warning + visibility | WARNING-once-per-org log on missing virtual key (reviewer finding #2(b)) |
| Mid-stream cancellation when budget hit | Let in-flight complete + audit over-spend + block next call (reviewer finding #3) |
| Per-tenant Anthropic billing scope | Explicitly out-of-scope; chargeback via audit rows only (reviewer finding #4) |
| Performance overhead disclosed | ~100-300ms/session; 1-3% of typical Opus session; documented in concept doc (reviewer finding #5) |
| `simple_client.py` bypass | PR-2 also wires `org_id` through simple_client (reviewer finding #6) |
| Per-model pricing test | `tests/test_claude_pricing.py` asserts pricing-dict completeness vs `phase_config.py` (recommendation #1) |
| Dashboard provider split | `provider` field added to `details_json` (recommendation #2) |
| Wrapper telemetry counters | Prometheus counter on PR-2; Grafana panel on PR-3 (recommendation #3) |
| v1.3 swap-path doc | "When to revisit Option B" section in concept doc lists the four bug numbers (recommendation #4) |
| CLAUDE.md convention | "Pass `org_id` when web-server context has one" added to CLAUDE.md in PR-2 (recommendation #5) |

No deviations from brainstorm intent — refinements tighten the
design without changing scope. Option B explicitly considered +
rejected for documented upstream-bug reasons; the v1.3 re-evaluation
trigger is documented so the decision is revisitable.
