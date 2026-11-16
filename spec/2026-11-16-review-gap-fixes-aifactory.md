---
status: draft
issue: 2047
intent: intent/2026-11-16-review-gap-fixes-aifactory.md
---

# Spec: seed failover provider logins

## Design
`cred_files_for_models` adds, for each model's provider, the providers in `core.provider_failover.enabled_failover_chain` (chain filtered by the operator allowlist), then maps them to login files as before. A bad AIFACTORY_RUNTIMES value is ignored here (gating rejects it when the provider is built).

## Alternatives rejected
- Seed every login: undoes #2025.
- Set AIFACTORY_FALLBACK_MODEL to a non-claude model: it only forces sonnet on the retry path.

## Risks
Claude is always an enabled failover target, so every Job now also gets the Claude login (even ollama-only runs); the #2025 tests are updated for that.
Operators with codex/antigravity enabled get those logins in all-claude Jobs again; that is the price of failover working.

## Verification
Regression test: default allowlist seeds claude only; codex,antigravity enabled adds their logins.
