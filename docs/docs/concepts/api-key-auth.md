---
title: API-key auth (opt-in)
sidebar_position: 13
---

# Opt-in direct API-key auth

AIFactory is **OAuth-only by default.** Any `ANTHROPIC_API_KEY` present in the
environment is **scrubbed from agents** so it can never silently bill the
Anthropic API. Operators whose intended billing model *is* a direct API key
(no Claude subscription) opt in with a single flag.

This is a safety default, not a limitation: on a shared or multi-tenant host, a
stray key in the environment should never quietly run up an API bill.

## The two modes

### OAuth (default, safe)

- Billing is tied to a Claude subscription via an OAuth token.
- A stray `ANTHROPIC_API_KEY` is **blanked before agents run** — it is excluded
  from the SDK subprocess environment.
- Safe for shared and multi-tenant deployments.

### Direct API key (opt-in)

- Billing is tied to an Anthropic API account, separate from any subscription.
- Useful for: organizations on an API-only agreement, per-team cost-center
  chargeback with a direct key, or headless deployments with no interactive
  login.
- The key is accepted as an auth token **and** passed through to agents.

## Enabling it

Set the flag on the web server (`.env` or pod env) alongside the key:

```bash
AIFACTORY_ALLOW_API_KEY=1
ANTHROPIC_API_KEY=sk-ant-...
```

Accepted truthy values: `1`, `true`, `yes`, `on`.

With the flag on:

1. OAuth remains the primary auth — the login flow is unchanged.
2. If `ANTHROPIC_API_KEY` is set, it is accepted as a fallback auth token.
3. The key is **passed through** to the agent SDK environment instead of being
   blanked.

With the flag off or unset (the default):

1. OAuth is the only auth.
2. `ANTHROPIC_API_KEY` is scrubbed from the agent environment.
3. Any attempt to use a stray key in an agent fails safely.

## How it works

A single gate — `core.auth.api_key_auth_enabled()` — decides whether the key is
accepted as a token and whether it is passed through to agents. By default
`ANTHROPIC_API_KEY` is excluded from the SDK passthrough list and only added
back dynamically when the flag is set.

## See also

- [Multi-provider routing](./multi-provider) — pick a model per phase
- [LiteLLM gateway](./litellm-gateway) — per-org budget + rate limits (Enterprise)
- [Configuration Reference](../configuration-reference)
