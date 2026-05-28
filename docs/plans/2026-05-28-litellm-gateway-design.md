# Design — LiteLLM gateway (Epic #35 #38)

> Locked from super-brainstorm 2026-05-28 + reviewer audit. Implementation
> in 3 PRs after sign-off.

## Scope (revised after reviewer audit)

**v1.1 routes non-Claude providers through LiteLLM.** Claude calls
continue to use the Claude Agent SDK directly — the SDK spawns the
`claude` CLI as a subprocess (not an HTTP client), and the CLI
speaks Anthropic-format `POST /v1/messages` which is not
wire-compatible with LiteLLM's OpenAI-format `POST /v1/chat/completions`.

| Provider | Gateway routing in v1.1 | Audit coverage |
|----------|-------------------------|----------------|
| Claude (via Claude Agent SDK) | ❌ Direct | Existing chain audit only (#26 P5 + #43 anchor); NOT subject to LiteLLM budget/allowlist enforcement |
| OpenAI / OpenAI-compatible (LM Studio, vLLM, OpenRouter, etc.) | ✅ Via gateway | Full LiteLLM enforcement + audit hook |
| Codex CLI | ✅ Via gateway | Full LiteLLM enforcement + audit hook |
| Gemini | ✅ Via gateway | Full LiteLLM enforcement + audit hook |
| Ollama | ✅ Via gateway | Full LiteLLM enforcement + audit hook |

**Implication for SOC2 CC7.2 / ISO 27001 A.12.4.1 evidence:** Claude
calls retain audit-chain coverage from #43 (the `audit_logs.action=
'claude.session.start' / claude.session.end'` events that
`agent_service` already writes are signed by the daily anchor), but
they don't get per-tenant budget / rate-limit / allowlist enforcement
in v1.1. Documented as a known v1.1 limitation; closes in **v1.2 via
either (a) an in-process Claude-SDK enforcement wrapper in
`core/client.py` mirroring LiteLLM's enforcement, or (b) a LiteLLM
Anthropic-format passthrough endpoint if LiteLLM upstream adds one.**
Compliance team needs to sign off on this scope before #38 ships.

## Why we're doing this

v1.0's LLM provider abstraction (`apps/backend/providers/`) calls
Anthropic / OpenAI / Gemini / Ollama directly. Each provider class
owns its own HTTP client; there's no centralized place to enforce:

- **Per-tenant token budget** — "this org can spend $500/month max"
- **Per-tenant rate limit** — "this org can issue 60 LLM calls/minute"
- **Per-tenant model allowlist** — "this org can use Sonnet but not Opus"
- **Audit of prompt + response** — for SOC2 CC7.2 + ISO 27001 A.12.4.1
- **Cost observability** — "which tenant is driving the bill?"

#38 inserts LiteLLM (https://github.com/BerriAI/litellm) as an
in-cluster proxy between every provider and the actual LLM API.
LiteLLM gives us all the above for free + a Prometheus metrics
surface for Grafana dashboards.

This unblocks **#39** (Bedrock + Vertex providers): LiteLLM already
supports both; we just point at the gateway.

## Out of scope (explicit)

- **Multi-region LLM routing.** LiteLLM has it; v1.1 uses a single
  gateway instance per AIFactory deployment.
- **Cost-aware LLM selection** (cheapest model that meets quality
  bar). LiteLLM Pro feature; v1.1 keeps provider/model selection
  in AIFactory's existing config.
- **Per-tenant API keys for direct Anthropic billing.** Tenants in
  v1.1 share the deployment's API keys; per-tenant billing requires
  per-tenant Anthropic accounts (operator handles outside AIFactory).
- **Streaming response audit.** v1.1 audits the final response only,
  not per-chunk during streaming. Streaming-aware audit is v1.2.
- **Per-tenant LLM fine-tunes.** No model-customization plane in v1.1.
- **Caching.** LiteLLM has a Redis cache layer; deferred to v1.2 to
  avoid the privacy-vs-cost trade-off discussion.

## Locked decisions

### 1. Deployment — Helm sub-chart on official `litellm/litellm`

`Chart.yaml`:
```yaml
dependencies:
  - name: litellm
    version: "<pin to latest stable>"
    repository: https://litellm.github.io/litellm-helm
    condition: litellm.enabled
```

`values.yaml`:
```yaml
litellm:
  enabled: false                     # opt-in for v1.1
  # ... operator-overridable upstream litellm chart values
```

When `litellm.enabled=false` (default), no LiteLLM pods deploy and
the existing direct-provider path stays in place. Existing
deployments are byte-for-byte unchanged until the operator opts in.

When `true`, the upstream LiteLLM chart deploys with a default
config that points at the AIFactory deployment's configured
provider keys (mounted from Secret).

**Why sub-chart not standalone?** Reduces operator setup steps by
50% (one `helm install` covers everything). Same pattern as the
Postgres sub-chart we already include for dev convenience.

**Why not sidecar?** Multi-replica web pods each get their own
LiteLLM = duplicate budget counters + inconsistent rate-limit
enforcement. Sub-chart deploys ONE LiteLLM Service that all
replicas share, so budgets + limits are globally consistent.

### 2. Provider integration — swap `base_url` to gateway for the 4 OpenAI-compatible providers

LiteLLM exposes an OpenAI-API-compatible endpoint. The 4 providers
that already use OpenAI-format HTTP (OpenAI-compatible, Codex,
Gemini, Ollama) get a small change: read `LITELLM_GATEWAY_URL` from
env and swap `base_url` from the native endpoint to the gateway.

```python
# providers/openai_compatible.py (sketch — same pattern for codex / gemini / ollama)
gateway_url = os.environ.get("LITELLM_GATEWAY_URL")
if gateway_url:
    base_url = gateway_url
    # The original model name is preserved; LiteLLM handles the
    # provider-routing on its side based on the model prefix
    # (gpt-* → OpenAI, gemini-* → Vertex/Google, ollama-* → Ollama).
else:
    base_url = "<provider's native endpoint>"
```

**Claude provider stays direct.** `providers/claude.py` wraps
`ClaudeSDKClient` from `claude_agent_sdk`, which spawns the
`claude` CLI subprocess. Setting `ANTHROPIC_BASE_URL` points the CLI
at the gateway, but the CLI sends Anthropic-format requests and
LiteLLM speaks OpenAI-format — wire-incompatible. v1.1 leaves
Claude calls unchanged; see Scope section above for the v1.2
follow-up plan.

**Why not a new `litellm` provider in the factory?** The model name
already encodes the provider (gpt-* / gemini-* / ollama-*); adding
a separate provider class would double the factory matrix. Cleaner
to keep one class per model family + redirect via env.

**Why not httpx MITM at the client layer?** Fragile (hostname
allowlist) + breaks for self-hosted endpoints with custom URLs.

The factory.py file does NOT change. Existing tests pass unchanged
because `LITELLM_GATEWAY_URL` is unset in test environments.

### 3. Budget + rate-limit storage — LiteLLM's own Postgres

LiteLLM ships with its own Postgres-backed budget + rate-limit
storage + admin API. AIFactory:

- **Writes config:** on Organization create / budget update, AIFactory
  calls LiteLLM's admin API (`POST /key/generate`,
  `POST /budget/update`) to provision per-tenant LiteLLM "virtual
  keys" with the configured budget + RPM limits.
- **Reads counters:** AIFactory's billing dashboard reads counters
  via LiteLLM's `/spend/user` endpoint. No primary-key storage on
  the AIFactory side.

**Enforcement happens INSIDE LiteLLM at request time.** AIFactory's
own AuditLog captures the `llm.call` event for compliance (separate
concern from budget enforcement).

**Why not AIFactory-side enforcement?** Pre-call hooks back to
AIFactory would add HTTP latency per LLM call + couple the two
services tightly. LiteLLM's native enforcement is the documented +
tested path.

**Operational note:** LiteLLM's Postgres is a NEW database (it ships
as a sub-chart Postgres or operator-supplied). Documented in the
concept doc; operators with strict DB-count policies can point
LiteLLM at their existing Postgres via the upstream chart's
`db.url` value.

**LiteLLM master-key handling (reviewer finding #2):** the admin API
requires a master key (`LITELLM_MASTER_KEY`). Stored as a
KMS-wrapped Secret using the existing `crypto/kms/` abstraction:

```yaml
litellm:
  enabled: true
  masterKeySecretRef:
    name: aifactory-litellm-master-key
    key: master-key            # the wrapped key value
```

The reconciler unwraps via KMS on each admin-API call (cheap; calls
are infrequent). Rotation runbook: re-wrap the new key + restart
the web pod + restart LiteLLM with the new value. Documented in
the concept doc. **Forbidden anti-pattern (matches #43 design):**
plaintext master key in a generic `aifactory-config` Secret. The
KMS-wrapping requirement is enforced at startup — the reconciler
refuses to call admin APIs without a successful unwrap.

Blast radius of master-key leak: an attacker can rotate / delete /
create LiteLLM virtual keys (full budget + allowlist control). They
cannot read prompts/responses (LiteLLM doesn't store those by
default in v1.1). Rotation cadence: same as the KMS root cadence
documented in #43 (typically annual).

### 4. PII redaction — regex high-confidence + opt-in operator config

`apps/backend/services/llm_pii_redactor.py` (new) applies built-in
regex patterns to prompt + response BEFORE writing to the audit row:

```python
_BUILTIN_PATTERNS = [
    # US SSN: 123-45-6789 (XXX-XX-XXXX hyphenated only;
    # bare 9-digit numbers are too false-positive-prone).
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    # Email: alice@corp.com
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
    # US phone: (555) 123-4567 (parens + hyphen forms; bare
    # 10-digit numbers excluded — too many false positives).
    (re.compile(r"\b\(\d{3}\)[ -]?\d{3}-\d{4}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b\d{3}-\d{3}-\d{4}\b"), "[REDACTED_PHONE]"),
]
```

**Credit card pattern dropped (reviewer finding #4):** the original
pattern `\b(?:\d[ -]*?){13,16}\b` matched any 13-16 digit numeric
string (IPv4 CIDRs, code identifiers, etc.), corrupting legitimate
prompt content without Luhn validation. v1.1 omits CC redaction;
operators with PCI data add Luhn-checked patterns via the
`extraRedactionPatterns` extension hook. Documented as a known
limitation; v1.2 ships a Luhn-validating CC pattern as a built-in.

Operators add custom patterns via Helm values:

```yaml
litellm:
  audit:
    extraRedactionPatterns:
      - pattern: 'ACC-\d{8}'         # internal account number
        replacement: '[REDACTED_ACCT]'
```

**Why not Presidio / NER?** Adds a 200-MB Presidio container
dependency + 50-200ms latency per LLM call. Defer to v1.2 when
the cost is justified. v1.1's regex catches the high-value patterns
that show up in real prompts.

**Scope: redaction applies ONLY to the audit row, NOT to what the
LLM sees (reviewer finding #4).** A high-sensitivity tenant whose
prompt contains PII still sends that PII to the LLM provider — this
is intrinsic to LLM use. The threat-model table below makes this
explicit. **v1.2 will add `litellm.audit.scrubBeforeSend` mode**
that applies the same redactor to the prompt BEFORE the LLM call,
at the cost of some prompt-quality degradation. Compliance team
should sign off on this scope for v1.1.

### 5. Audit shape — per-call metadata + truncated text

```python
# audit_logs row written by the LLM-call hook
{
    "id": "<uuid>",
    "org_id": "<org-uuid>",
    "user_id": "<user-uuid or 'agent' for autonomous calls>",
    "action": "llm.call",
    "resource_type": "llm",
    "resource_id": "<model-name>",  # e.g. "claude-opus-4-7"
    "classification": "confidential",  # Epic #35 #43 classification
    "details_json": {
        "model": "claude-opus-4-7",
        "input_tokens": 1200,
        "output_tokens": 450,
        "cost_usd": 0.0234,
        "latency_ms": 1847,
        "prompt_truncated": "...first 4KB after PII redaction...",
        "response_truncated": "...first 4KB after PII redaction...",
        "litellm_request_id": "<for cross-reference with LiteLLM logs>",
    }
}
```

Bounded to ~10KB per row. Operators wanting full prompt/response
storage opt in via `litellm.audit.fullTextCapture=true` which
switches to the encrypted-rows path (full text encrypted via the
existing EncryptedString column type from Epic #26 P2).

**Cost accuracy (reviewer recommendation #4):** `cost_usd` is a
LiteLLM estimate from its internal pricing table, which lags
provider price changes by days/weeks. The audit row includes
`cost_source: "litellm_estimate"` in `details_json` so chargeback
queries can distinguish "approximate" from "authoritative" (the
latter requires provider invoices). Documented in the concept doc.

**Streaming response audit (reviewer finding #3):** the hook fires
on response COMPLETION (the final assembled message). For abandoned
streams (client disconnect, timeout, task cancellation), the hook
DOES NOT FIRE — the audit row is missing. Two responses:
1. The provider catches abandonment paths (e.g. `asyncio.CancelledError`
   in `receive_response()`) and writes an `action='llm.call.abandoned'`
   audit row with whatever partial token-count + cost it has + a
   `truncated: true` flag.
2. For provider-side failures (5xx mid-stream), the catch-block
   writes an `action='llm.call.failed'` row with the error.

Result: 100% of LLM-call attempts produce an audit row of some
shape, even abandonment/failure. The concept doc documents the 3
action variants + how to query for each.

### 6. Model allowlist — per-org

`organizations.allowed_models` (new JSONB column, default `["*"]`
for backward compat — all models allowed when isolation isn't
configured).

```sql
ALTER TABLE organizations
  ADD COLUMN allowed_models JSONB NOT NULL DEFAULT '["*"]';
```

On Organization create / update, AIFactory calls LiteLLM's admin API
to update the per-tenant key's `models` field. LiteLLM rejects
requests for non-allowed models with `400 model not in allowlist`,
which the provider class catches + re-raises as
`ModelNotAllowedError` (typed, surfaces in agent error logs).

**Why JSONB array, not separate table?** Allowlist is read on every
LLM call (via LiteLLM, not from AIFactory's DB) and updated rarely.
A separate table = extra join. JSONB array on the org row is
ergonomic + indexable when needed.

### 7. Dashboards — per-tenant cost + throughput + rate-limit headroom

Three Grafana panels ship in `charts/aifactory/dashboards/litellm.json`:

| Panel | Metric source | Visualization |
|-------|---------------|---------------|
| Per-tenant cost over time | LiteLLM Prometheus `litellm_total_spend{user="<org-uuid>"}` | Stacked area, grouped by model |
| Tokens-per-second per tenant | `rate(litellm_total_tokens{user="<org-uuid>"}[5m])` | Line per tenant |
| Rate-limit headroom | `litellm_remaining_requests / litellm_max_requests` × 100 | Gauge per tenant |

Operators import via `kubectl create configmap grafana-dashboard-litellm
--from-file=charts/aifactory/dashboards/litellm.json -n monitoring`.

Documented in the concept doc with a screenshot of each panel.

### 8. Failure mode — fail-closed

When the LiteLLM gateway is unreachable (timeout / 5xx / DNS
failure):

- Provider raises `LiteLLMGatewayUnavailableError`.
- Agent task fails with a clear, operator-visible error.
- **NO silent fallback** to direct provider calls. The whole point
  of the gateway is the audit + budget + allowlist enforcement;
  bypassing on failure defeats the purpose.

```python
# providers/claude.py (failure path)
try:
    response = await client.messages.create(...)
except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
    if os.environ.get("LITELLM_GATEWAY_URL"):
        raise LiteLLMGatewayUnavailableError(
            f"LiteLLM gateway unreachable: {exc}. Task failed; check "
            f"gateway status (kubectl logs -n aifactory -l app=litellm)."
        ) from exc
    raise
```

Operators alert on `litellm_up == 0` in Prometheus. Documented in
the concept doc as the "what to monitor" section.

**Why not fail-open?** Every gateway outage would silently lose
audit data + bypass budget enforcement. Compliance team would
never accept this default. An opt-in operator override is
acknowledged as a v1.2 possibility but not built in v1.1.

**Circuit breaker for transient errors (reviewer recommendation #1):**
fail-closed-on-every-error would mean a 10-second LiteLLM pod
reschedule cancels every in-flight agent task. The provider wraps
LiteLLM calls in a small retry layer:

```python
# 3 retries with exponential backoff: 100ms, 200ms, 400ms.
# Total worst-case retry budget: 700ms before failing the task.
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.1, max=0.4))
async def _call_litellm(client, payload):
    return await client.post(...)
```

Transient errors (single-pod restart, brief DNS flap) recover
silently. Sustained outages (3 consecutive failures = ~700ms wall
clock) fail the task with `LiteLLMGatewayUnavailableError`.
Operators alert on `litellm_up == 0` for the longer-term outage
case. Documented circuit-breaker config in the concept doc.

## Failure-safe contract

Same pattern as #40/#41/#42/#43:
- Every audit-write wraps in `try/except`. A failed audit write
  logs WARNING but does NOT block the LLM call (the call already
  succeeded; the audit is a separate post-write concern).
- A failed PII-redaction pass logs WARNING + writes the unredacted
  text. Documented as a known risk in the concept doc.
- A failed LiteLLM admin API call (during org create / budget update)
  logs WARNING + retries on the next reconciler tick (per #36's
  reconciler pattern; this PR's tenant_reconciler dependency hooks
  in).

## Threat model

| Threat | Pre-#38 | Post-#38 |
|--------|---------|----------|
| Compromised agent prompt extracts secrets via LLM | Undefended | Partial (regex redaction in audit-row only; the LLM still sees plaintext — intrinsic to LLM use; v1.2 `scrubBeforeSend`) |
| Org A exhausts deployment-wide LLM budget (non-Claude) | Undefended | Defended (per-tenant budget at gateway) |
| Org A exhausts deployment-wide LLM budget (Claude) | Undefended | **Still undefended in v1.1** — Claude bypasses gateway (Scope); v1.2 closes |
| Org A's runaway loop costs $10k overnight (non-Claude) | Undefended | Defended (per-tenant rate-limit) |
| Org A's runaway loop costs $10k overnight (Claude) | Undefended | **Still undefended in v1.1** — Claude bypasses; v1.2 closes |
| Org A uses gpt-5 when paid only for gpt-4 | Undefended | Defended (per-tenant allowlist for OpenAI-compat providers) |
| Org A uses claude-opus when paid only for claude-sonnet | Undefended | **Still undefended in v1.1** — Claude allowlist deferred to v1.2 |
| Audit log missing the LLM call that exposed data (non-Claude) | Undefended (no LLM-call audit) | Defended (every call captured incl. abandoned/failed streams) |
| Audit log missing the LLM call that exposed data (Claude) | Partial (existing `claude.session.*` audit) | Same (no v1.1 change for Claude) |
| Gateway outage → audit gap for non-Claude | N/A | Defended (fail-closed: no calls without audit; 700ms retry budget for transient errors) |
| Operator misconfigures redaction → PII in audit | N/A | Documented risk; ops review of redaction patterns required |

## Implementation plan — 3 PRs

### PR-1 — Provider env redirect + LiteLLMGatewayUnavailableError + tests

- Each provider class reads `LITELLM_GATEWAY_URL`; redirects `base_url`
  when set.
- New `LiteLLMGatewayUnavailableError` exception class.
- Unit tests with mocked LiteLLM endpoint.
- No DB changes; no Helm changes; no operator-visible behavior change
  (env var is unset in default deployments).

### PR-2 — Schema + audit hook + per-org allowlist + virtual-key lifecycle

**Merge constraint:** blocked on **#36 PR-1** landing on `dev`
(this PR's `tenant_reconciler` extension is a no-op call without
the framework from #36). The PR description must call this out
explicitly so CI doesn't green-light merging early.

- Alembic migration: `organizations.allowed_models JSONB` column.
- `llm_pii_redactor.py` with built-in regex patterns + operator
  extension hook.
- LLM-call audit writer: after every provider response, write
  `audit_logs` row with `action='llm.call'` (or `.abandoned` /
  `.failed` per streaming-audit spec above), classification
  `'confidential'`, `details_json.cost_source='litellm_estimate'`.
  Wrapped in try/except so an audit-write failure doesn't fail the
  calling task.
- `tenant_reconciler` extension to sync per-tenant LiteLLM admin-API
  config from AIFactory's `organizations.allowed_models`.
- **Virtual-key lifecycle (reviewer recommendation #5):**
  - Org create → reconciler issues `POST /key/generate` with the
    org's `allowed_models` + budget.
  - Org soft-delete → reconciler disables key via
    `PUT /key/update` with `budget_duration=0` (immediate block;
    audit row records the disable).
  - Org hard-delete (day 30 per #36) → reconciler issues
    `DELETE /key/delete`.
  - Drift recovery (LiteLLM DB restored from backup): the periodic
    reconcile-all sweep compares AIFactory org state to LiteLLM
    key state via `/key/list`; creates missing, revokes orphans.
- Tests against in-process SQLite + a mock LiteLLM admin endpoint.

### PR-3 — Helm sub-chart + Grafana dashboards + concept doc

- `Chart.yaml` dependency on `litellm/litellm` **pinned to
  starting version 1.x.y (look up current stable at PR-3 time;
  do not ship `<pin to latest>` placeholder).** A `renovate.json`
  or Dependabot entry tracks `1.x` patch bumps automatically;
  minor-version bumps go through a staging test of the budget /
  allowlist API before merging (reviewer recommendation #3).
- `values.yaml` `litellm:` block with operator-overridable upstream
  values + AIFactory-specific options (audit, PII patterns,
  failure-mode, master-key Secret ref).
- `helm dep update` runs as part of CI.
- `charts/aifactory/dashboards/litellm.json` Grafana dashboard.
- `docs/docs/concepts/litellm-gateway.md` user-facing concept doc.
- CHANGELOG: strike v3.0 limitation #6 ("LLM-call audit deferred to
  v3.1 LiteLLM gateway") **with the scope caveat that Claude calls
  remain on chain-audit-only enforcement until v1.2.**

## Decision audit summary

8 of 8 brainstorm decisions taken on recommended options. Reviewer
audit pass surfaced 4 critical findings + 5 recommendations; all
baked in above:

| Finding | Resolution |
|---------|------------|
| **Claude Agent SDK incompatible with `base_url` swap** | Scope revised: Claude calls stay direct in v1.1 (chain-audit-only); LiteLLM enforcement covers OpenAI / Codex / Gemini / Ollama. Documented Scope section at top + Threat-model table caveat. v1.2 closes via in-process Claude wrapper or LiteLLM Anthropic-passthrough |
| **LiteLLM master key has no storage/rotation plan** | Locked: KMS-wrapped Secret via existing `crypto/kms/` abstraction; reconciler unwraps per admin-API call; rotation runbook mirrors #43 anchor-key cadence; plaintext-key anti-pattern explicitly forbidden (§3) |
| **Streaming response audit gap** | Locked: 3 action variants (`llm.call`, `.abandoned`, `.failed`) covering complete / cancelled / errored streams; 100% of LLM-call attempts produce an audit row (§5) |
| **PII regex correctness + LLM-still-sees-plaintext** | CC pattern dropped (too false-positive-prone without Luhn); SSN / email / phone tightened to less-greedy forms; explicit out-of-scope statement that LLM sees plaintext + v1.2 `scrubBeforeSend` mode documented (§4) |
| Circuit breaker for transient errors | 3-retry exponential backoff (700ms total budget) before fail-closed (§8) |
| Cross-epic ordering | PR-2 explicitly blocked on #36 PR-1 (§implementation plan) |
| LiteLLM version pin + bump cadence | Placeholder replaced with explicit "look up 1.x.y at PR-3 time"; renovate/dependabot for patch bumps; minor-version staging test required |
| Cost accuracy disclosure | `cost_source: "litellm_estimate"` field in `details_json` so chargeback queries distinguish approximate from authoritative (§5) |
| Virtual-key lifecycle on org delete + drift recovery | Soft-delete disables, hard-delete deletes, periodic reconcile-all sweep reconciles drift after DB restore (§PR-2) |

No deviations from brainstorm intent — refinements tighten the
design without changing scope (except the Claude exclusion, which
was a forced honest revision from "every provider" to "4 of 5
providers" based on architectural reality).
