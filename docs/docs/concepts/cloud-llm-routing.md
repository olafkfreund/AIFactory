---
title: Cloud LLM routing (Bedrock + Vertex)
sidebar_position: 13
---

# Cloud LLM routing — Amazon Bedrock and Google Vertex AI

> *Banks and regulated enterprises routinely procure LLMs through AWS or GCP contracts. This page explains how AIFactory routes to those backends, what the operator must configure, and where the boundaries lie.*

## When to use Bedrock or Vertex

Use Bedrock or Vertex when **any** of these apply:

- Your organisation has an **existing AWS or GCP enterprise agreement** and adding a new Anthropic / OpenAI vendor would require a procurement exception.
- **Data residency** requirements mandate that inference traffic never leave a specific region or cloud — your AWS region already meets the policy.
- Your security team has completed a **Bedrock or Vertex BAA** and does not want to open a new one for the direct Anthropic API.
- You want **per-request cost attribution** to flow through your existing AWS Cost Explorer or GCP Billing dashboards rather than a separate Anthropic bill.

You do **not** need Bedrock or Vertex if the direct Anthropic API is acceptable to procurement and compliance; the Claude provider already handles that path with no extra configuration.

## Architectural pattern

AIFactory does not call Bedrock or Vertex directly. The routing goes through the LiteLLM gateway (Epic #35 #38):

```
AIFactory agent
     |
     | OpenAI-format POST /v1/chat/completions
     v
LiteLLM gateway (LITELLM_GATEWAY_URL)
     |
     | AWS SigV4 / GCP OIDC
     |
     +---> Amazon Bedrock InvokeModel API
     |
     +---> Google Vertex AI predict endpoint
```

LiteLLM natively understands `bedrock/` and `vertex_ai/` model-name prefixes. When AIFactory sends a request with `"model": "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"` to the gateway, LiteLLM resolves the backend, signs the request, and forwards it.

This means:

- No new Python provider classes are needed in AIFactory.
- All of LiteLLM's per-tenant budget enforcement, rate-limiting, allowlisting, and audit hooks (from Epic #35 #38) work identically regardless of the underlying cloud backend.
- Operators swap backends by changing model strings in `values.yaml`, not by redeploying AIFactory.

## The `LITELLM_GATEWAY_URL` requirement

AIFactory validates at provider-construction time that `LITELLM_GATEWAY_URL` is set before routing any `bedrock/*` or `vertex_ai/*` model. Without it the request would reach `api.openai.com` with a cloud-prefixed model name and produce a confusing 404. The guard raises a clear error instead:

```
ValueError: Bedrock / Vertex models require LITELLM_GATEWAY_URL to be configured
(LiteLLM is the routing layer for cloud-provider models). Set LITELLM_GATEWAY_URL
to your LiteLLM proxy address (e.g. http://litellm:4000).
See docs/concepts/cloud-llm-routing.md.
```

Set `LITELLM_GATEWAY_URL` in your Helm values or deployment environment.

## The Claude-on-Bedrock / Claude-on-Vertex caveat

The Claude Agent SDK has its own direct cloud path controlled by env vars:

- `CLAUDE_CODE_USE_BEDROCK=1` — Claude SDK routes directly to Bedrock, bypassing LiteLLM.
- `CLAUDE_CODE_USE_VERTEX=1` — Claude SDK routes directly to Vertex AI, bypassing LiteLLM.

When these flags are set in the agent process environment, the Claude Agent SDK's built-in cloud integration takes over and AIFactory's LiteLLM enforcement (budget, audit, allowlist) does **not** apply to those Claude calls.

**v1.1 keeps this path.** If you want LiteLLM enforcement to cover Claude-on-Bedrock or Claude-on-Vertex, set those models with the `bedrock/` or `vertex_ai/` prefix in AIFactory's model config (not via the SDK env vars). The LiteLLM gateway then mediates those calls.

A future release (v1.2) will close this gap via an in-process Claude wrapper that routes all Claude traffic through LiteLLM regardless of SDK flags.

## Operator recipes

### Deploy LiteLLM with a Bedrock backend

Configure LiteLLM to accept requests and forward them to Bedrock. Refer to the [LiteLLM Bedrock documentation](https://docs.litellm.ai/docs/providers/bedrock) for the full setup, including IAM role requirements and supported model IDs.

Key configuration points:

- Set the AWS region via `AWS_DEFAULT_REGION` in the LiteLLM pod env.
- Grant the LiteLLM pod's IAM role (or service account, for IRSA on EKS) `bedrock:InvokeModel` on the model ARNs you intend to use.
- Add your model list to LiteLLM's `config.yaml` under `model_list` using the `bedrock/` prefix.

Example LiteLLM model entry for Bedrock:

```yaml
model_list:
  - model_name: bedrock/anthropic.claude-sonnet-4-20250514-v1:0
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-20250514-v1:0
      aws_region_name: us-east-1
  - model_name: bedrock/meta.llama3-1-70b-instruct-v1:0
    litellm_params:
      model: bedrock/meta.llama3-1-70b-instruct-v1:0
      aws_region_name: us-east-1
```

### Deploy LiteLLM with a Vertex AI backend

Refer to the [LiteLLM Vertex AI documentation](https://docs.litellm.ai/docs/providers/vertex) for credential setup (Workload Identity or service account JSON).

Example LiteLLM model entry for Vertex:

```yaml
model_list:
  - model_name: vertex_ai/gemini-2.5-pro
    litellm_params:
      model: vertex_ai/gemini-2.5-pro
      vertex_project: my-gcp-project
      vertex_location: us-central1
  - model_name: vertex_ai/claude-sonnet-4@20250514
    litellm_params:
      model: vertex_ai/claude-sonnet-4@20250514
      vertex_project: my-gcp-project
      vertex_location: us-east5
```

### Configure AIFactory

Set `LITELLM_GATEWAY_URL` to the address of your LiteLLM deployment and use cloud-prefixed model strings:

```yaml
# In your AIFactory Helm values or pod env
env:
  LITELLM_GATEWAY_URL: "http://litellm.aifactory.svc.cluster.local:4000"

# Example task model configuration
phaseModels:
  coding: "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"
  qa: "vertex_ai/gemini-2.5-pro"
```

### Verify routing works

After deployment, confirm a request reaches the cloud backend:

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  "$LITELLM_GATEWAY_URL/v1/chat/completions" \
  -d '{
    "model": "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
    "messages": [{"role": "user", "content": "say hello"}],
    "max_tokens": 10
  }' | jq '.choices[0].message.content'
```

A successful response confirms LiteLLM is reaching Bedrock. If you see an AWS error, check IAM permissions. If you see a LiteLLM error about an unknown model, verify your `model_list` in LiteLLM's config.

## Per-tenant IAM and Workload Identity via Epic #35 #36

When the tenant-isolation reconciler (#36) is active, each tenant Namespace gets its own ServiceAccount annotated with:

- **EKS:** an IRSA-bound IAM role permitting `bedrock:InvokeModel` on the tenant's allowed model ARNs.
- **GKE:** a Workload-Identity-bound GCP service account with `roles/aiplatform.user`.

LiteLLM's `team_id` → cloud-account mapping is updated by the reconciler on Organisation create and role-change events. When #36 is not deployed (single-tenant or operator opt-out), all tenants share the deployment-wide cloud credentials.

See the tenant-isolation concept doc for details on the reconciler and its configuration.

## Per-tenant accounting

In v1.1, cost attribution happens at the **LiteLLM accounting level**: LiteLLM records spend per `team_id` (one per AIFactory organisation) against the deployment's cloud account. This means:

- A single AWS or GCP bill covers all tenants' Bedrock/Vertex usage.
- Per-tenant cost is visible in LiteLLM's usage dashboard, not in AWS Cost Explorer or GCP Billing per-account.
- Cross-tenant cost isolation at the AWS/GCP-bill level (separate AWS accounts per tenant) is a v1.2 concern — it requires per-tenant AssumeRole chaining which is out of scope for #36's initial reconciler.

Operators should communicate this trade-off to tenants who have contractual per-AWS-account cost reporting requirements.

## Bedrock guardrails and Vertex safety filters

Both backends expose content-filtering and safety controls:

- **Bedrock Guardrails**: configurable via LiteLLM's `guardrailConfig` passthrough field in the model_list entry.
- **Vertex AI safety filters**: configurable via `safety_settings` in the LiteLLM Vertex model entry.

AIFactory does not pre-configure these. Operators tune them in LiteLLM's `config.yaml` alongside the model list entries. Refer to the LiteLLM documentation for the exact field names per backend.
