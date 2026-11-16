---
status: draft
issue: 2025
intent: intent/2026-11-16-2025-seed-creds-by-provider.md
---

# Spec: seed only the CLI logins a build Job uses

## Design

`build_backend.cred_files_for_models(models)` maps each model to a provider with `phase_config.infer_provider_from_model` and keeps the `_CLI_CRED_FILES` entries for those providers (claude, codex, copilot, antigravity=gemini). Other providers (ollama, openai-compatible...) need no file.

- Build Job: models are `phase_config._resolve_phase_model` for spec, planning, coding, qa, qa_fixer, read from the spec dir's task metadata (same precedence the run uses).
- Spec Job: `[model, "sonnet"]` (the spec pipeline always summarizes phases with sonnet, so Claude stays), replacing its Copilot-only filter, so one rule serves both.
- `_inject_seed_creds` already takes `cred_files` (from #1932); no change to it.

## Alternatives rejected

- Drop only Copilot in build Jobs: leaves Codex/Gemini/Claude logins readable when unused; the issue asks for all providers.
- Parse the model string in build_backend: duplicates provider inference and would drift.

## Risks

A run that needs a login the phase models do not name (a late provider fallback) would lack it. `AIFACTORY_FALLBACK_MODEL` is honoured by `_resolve_phase_model`.

## Verification

Parametrized tests per provider combination for the build Job and the spec Job; existing kubejob and spec-job tests pass.
