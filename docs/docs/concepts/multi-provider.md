---
title: Multi-Provider
sidebar_position: 2
---

# Multi-Provider — pick the right model per phase

AIFactory routes each pipeline phase to its own model. You can plan with Claude Opus, code with a local Ollama qwen3:14b, and validate with Claude Sonnet — all in one task.

## Supported providers

| Provider | Models | Use case |
|---|---|---|
| **Anthropic** (via Claude Agent SDK) | Opus 4.x, Sonnet 4.x, Haiku 4.x | Default for planning + QA — highest quality, integrated with MCP servers |
| **Ollama** | `qwen3:*`, `llama3.x:*`, `deepseek-coder:*`, any local model | Cheap coding for routine tasks; runs offline |
| **OpenAI** | `gpt-4o`, `gpt-4.1`, `o3-mini` | Drop-in alternative to Anthropic where licensing or compliance prefers it |
| **Codex CLI** | OpenAI Codex via local CLI | Code-specialist phase |
| **Gemini CLI** | Google Gemini Pro 2.x | Sunsetting 2026-06-18 — see [migration notes](../wiki/troubleshooting#gemini-sunset) |
| **OpenAI-compatible** | LM Studio, vLLM, OpenRouter, Together, Groq, LocalAI | Any endpoint that speaks the OpenAI `/v1/chat/completions` shape |

## How routing works

Each task has a **phase profile** — a mapping from phase name to model string. Example:

```json
{
  "phaseModels": {
    "spec": "sonnet",
    "planning": "opus",
    "coding": "ollama:qwen3:14b",
    "qa": "sonnet",
    "qa_fixer": "sonnet"
  }
}
```

The backend's `phase_config.infer_provider_from_model()` parses the model string and picks the right provider:

- `sonnet`, `opus`, `haiku` → Claude Agent SDK
- `ollama:<model>` → Ollama
- `codex:<model>` → Codex CLI
- `gemini:<model>` → Gemini CLI
- `gpt-*`, `o3*` → OpenAI
- `<endpoint>:<model>` (with custom endpoint registered in Settings → LLM Providers) → OpenAI-compatible

## Where to configure

- **Per task** — Task Creation Wizard → Agent Profile dropdown
- **Per profile** — Settings → Agent Profile (create reusable profiles)
- **Per endpoint** — Settings → LLM Providers (register your endpoints, API keys are encrypted at rest)

## The rule we never break

Claude interactions **always** route through `apps/backend/core/client.py::create_client()`. Never raw `anthropic.Anthropic()`. This is enforced in code review and is the only way OAuth-token auth + MCP server integration + per-agent tool permissions all work together.
