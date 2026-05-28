# Design — Bedrock + Vertex provider routing (Epic #35 #39)

> Locked from super-brainstorm 2026-05-28. Smallest-scope design in
> Epic #35: 1-2 day Helm + docs PR after the #38 implementation
> lands.

## Why we're doing this

Banks usually buy Claude (and Llama / Mistral / Gemini) via Amazon
Bedrock or Google Vertex AI for procurement + data-residency
reasons — they have existing contracts with AWS / GCP and don't
want a new vendor on the books. v1.0 only supports direct Anthropic
+ OpenAI + Ollama; banks with Bedrock/Vertex mandates can't deploy
AIFactory.

#39 unblocks them. The original issue assumed two new Python
provider classes; that scope dissolved once #38's LiteLLM gateway
landed in the design pipeline.

## Scope revision (key insight)

**LiteLLM natively supports `bedrock/*` and `vertex_ai/*` model
prefixes.** Once #38 ships, configuring AIFactory to use Bedrock or
Vertex is purely a LiteLLM config + Helm values exercise. No
net-new Python provider classes are needed:

- `bedrock/anthropic.claude-sonnet-4-20250514-v1:0` → LiteLLM →
  Bedrock InvokeModel API
- `vertex_ai/gemini-2.5-pro` → LiteLLM → Vertex predict endpoint
- `bedrock/meta.llama3-1-70b-instruct-v1:0` → LiteLLM → Bedrock
- `vertex_ai/claude-sonnet-4@20250514` (Anthropic-on-Vertex) →
  LiteLLM → Vertex predict endpoint

Every Bedrock / Vertex call inherits all of #38's enforcement:
per-tenant budget, rate-limit, allowlist, audit hook with 3 action
variants.

**The Claude exception from #38 applies here too:** if an operator
wants to use Anthropic-on-Bedrock via the Claude Agent SDK directly
(setting `CLAUDE_CODE_USE_BEDROCK=1` in the agent's env), those
calls bypass LiteLLM. v1.1 keeps the existing Claude SDK path for
Claude-on-Bedrock; LiteLLM enforcement covers everything else.

## Out of scope (explicit)

- **Per-cloud cost-attribution dashboards.** LiteLLM's per-tenant
  cost dashboards (from #38) work the same regardless of underlying
  cloud. Per-AWS-account or per-GCP-project breakdown is a v1.2
  concern.
- **Bedrock guardrails / Vertex safety filters.** LiteLLM exposes
  these via passthrough config; documented as operator-tunable but
  not pre-configured in the chart.
- **Live cloud CI tests.** v1.1 uses mocked LiteLLM responses; the
  real-cloud smoke test is the operator's post-deploy step.
- **Per-tenant Bedrock/Vertex API key billing.** Tenants share the
  deployment's cloud account in v1.1 (per-tenant cost attribution
  via LiteLLM's accounting, not via separate AWS/GCP accounts).

## Locked decisions

### 1. Routing — LiteLLM gateway only

No new `BedrockProvider` / `VertexProvider` Python classes. Models
are reached via LiteLLM with the standard `bedrock/*` and
`vertex_ai/*` prefixes. The existing `openai_compatible.py`
provider class handles the AIFactory → LiteLLM HTTP call (LiteLLM
exposes OpenAI-format on its side regardless of the underlying
backend).

**Why not new provider classes?** Reinventing budget / rate-limit
/ allowlist / audit for these two clouds would duplicate #38's
work + create three enforcement paths that drift over time. LiteLLM
is already battle-tested for both clouds.

**The Claude-on-Bedrock / Claude-on-Vertex caveat:** when an
operator sets `CLAUDE_CODE_USE_BEDROCK=1` or `CLAUDE_CODE_USE_VERTEX=1`
in the agent's env (existing Claude CLI flags), the Claude Agent
SDK routes through the cloud directly, bypassing LiteLLM —
same caveat as #38's Claude exclusion. Documented in the
concept doc; v1.2 closes via in-process Claude wrapper.

### 2. Credentials — per-tenant IAM / WI via #36's reconciler

When #36 (Tenant Isolation) ships, each tenant Namespace has its
own ServiceAccount with:
- **AWS:** an IRSA-bound IAM role (Bedrock `InvokeModel` allowed)
- **GCP:** a Workload-Identity-bound GCP service account (Vertex
  `predict` allowed)

LiteLLM, running in the deployment-default namespace, configures
per-tenant cloud-account routing via its `team_id` → AWS-account /
GCP-project mapping. The reconciler (#36 + #38) updates this
mapping on Organization create / role-change.

**Without #36 (operator opts out of isolation):** all tenants
share the deployment-wide cloud credentials. Cost attribution
happens at the LiteLLM accounting level only, not at the
AWS/GCP-bill level. Documented operator trade-off.

**For non-EKS/GKE clusters:** static AWS access keys / GCP service
account JSON via Helm Secret. Documented per-cloud examples in the
concept doc.

### 3. CI strategy — Mock LiteLLM responses

Tests live in `tests/litellm/test_bedrock_vertex_routing.py`. They
configure a mock LiteLLM endpoint that returns canned responses
for `bedrock/anthropic.claude-*` and `vertex_ai/gemini-*` model
names. Assertions:

- AIFactory sends the right request shape (OpenAI-format with the
  correct `model` prefix).
- The audit hook from #38 fires with `details_json.model` matching
  the cloud-prefixed name.
- The per-tenant allowlist correctly rejects models NOT in the
  org's allowed list (e.g. `bedrock/cohere.command-*` for an org
  scoped to Anthropic-only).

Zero cloud calls; runs in standard CI; no LocalStack / GCP
emulator dependency.

**Real-cloud smoke is the operator's post-deploy step.** The
concept doc gives a copy-pasteable curl + python script the
operator runs once to verify routing works.

### 4. Scope — Helm config + concept doc + CHANGELOG

The single PR closing #39 ships:

- **`values.yaml`** — example LiteLLM model lists for Bedrock + Vertex
  in the `litellm:` block (commented out by default; operators
  uncomment + set their model list).
- **`charts/aifactory/dashboards/litellm.json`** — extends the #38
  dashboard with a "by cloud backend" panel (Bedrock / Vertex /
  Anthropic-direct / OpenAI-direct).
- **`docs/docs/concepts/bedrock-vertex.md`** — concept doc:
  - Why use Bedrock / Vertex (procurement / data-residency).
  - Cloud-account setup (IRSA / WI / static key per cluster type).
  - Example LiteLLM model entries.
  - Anthropic-on-Bedrock caveat (use Claude SDK with env flag, OR
    LiteLLM for budget/audit enforcement — pick one per deployment).
  - Verification curl snippet.
- **`docs/docs/concepts/litellm-gateway.md`** — cross-link to the
  new bedrock-vertex page from the #38 concept doc.
- **`docs/sidebars.ts`** — add bedrock-vertex entry.
- **`CHANGELOG.md`** — Epic #35 #39 ✅ closed entry; cross-link to
  #38's enforcement story.

**No tests beyond what #38 already covers** (the routing-shape
assertions are integration tests for #38's audit hook; they
exercise Bedrock/Vertex prefixes via the existing mock LiteLLM).

**No new Python code.**

## Threat model

| Threat | Pre-#39 | Post-#39 |
|--------|---------|----------|
| Bank with Bedrock-only procurement can't deploy AIFactory | Undefended | Defended (LiteLLM routes to Bedrock) |
| Operator misconfigures Bedrock region → silent fallback to wrong region | N/A | Documented in concept doc; LiteLLM rejects invalid model strings |
| Per-tenant cost attribution breaks across cloud backends | N/A | Inherits #38's per-tenant LiteLLM accounting; works the same regardless of backend |
| Cloud IAM role leaks → cross-org cost charge | Undefended (no isolation) | Defended when #36 is on (per-tenant IAM role); operator trade-off when off |

## Implementation plan — 1 PR

**Blocked on:** #38 PR-3 landing on `dev` (this PR's chart additions
extend #38's `litellm:` block).

**Optional dependency:** #36 PR-3 (Helm tenant block) for per-tenant
cloud credentials; without it, the doc covers single-credentials
deployment mode.

Single PR closing #39:

- `values.yaml` Bedrock + Vertex examples in the `litellm.model_list`
  section.
- Grafana dashboard panel addition.
- `docs/docs/concepts/bedrock-vertex.md` + sidebar entry.
- Cross-link in the LiteLLM concept doc.
- CHANGELOG entry.

Estimated effort: 1-2 days.

## Decision audit summary

4 of 4 brainstorm decisions taken on recommended options. No
reviewer audit needed for this design — the scope is small enough
that a reviewer pass would surface only the same caveats already
addressed by #38's audit (Claude SDK bypass, KMS master key,
streaming gaps, etc.) which are inherited unchanged.

The original issue's task list (new BedrockProvider + VertexProvider
classes, LocalStack CI, per-tenant ExternalSecret) is explicitly
descoped — superseded by #38's LiteLLM-as-enforcement-plane
architecture. The CHANGELOG entry should note this honestly so
auditors see the design evolution.
