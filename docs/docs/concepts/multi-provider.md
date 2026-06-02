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
| **Codex CLI** | `gpt-5.3-codex` and other OpenAI Codex models via the local CLI | Fastest reliable agentic coding (see the [provider benchmark](../showcase/benchmark-results)) |
| **GitHub Copilot CLI** | `copilot:claude-sonnet-4.5`, `copilot:claude-sonnet-4`, `copilot:gpt-5` | Run builds on your existing GitHub Copilot subscription — no extra API key. Copilot is a router over Claude/GPT-5 backends |
| **Gemini CLI** | Google Gemini 3.x Pro | Capable agentic coding; the isolated worktree is trusted automatically so it can edit files |
| **Ollama** | `qwen2.5-coder:*`, `qwen3-coder:*`, `llama3.x:*`, `deepseek-coder:*`, any local model | Free, offline, air-gapped coding. Needs an adequately-sized model + GPU — see [Local models: sizing & hardware](#local-models-sizing--hardware) |
| **OpenAI** | `gpt-4o`, `gpt-4.1`, `o3-mini` | Drop-in alternative where licensing or compliance prefers it |
| **OpenAI-compatible** | LM Studio, vLLM, OpenRouter, Together, Groq, LocalAI | Any endpoint that speaks the OpenAI `/v1/chat/completions` shape |
| **OpenCode CLI** *(community / self-host tier)* | `opencode:<provider/model>`, e.g. `opencode:anthropic/claude-sonnet-4-5` | Run builds through the [OpenCode](https://opencode.ai) CLI runtime. **Not enterprise-certified** — its model catalogue comes from the remote `models.dev` registry, so models can change/disappear (there is no guaranteed free default). See the [tier note](#provider-tiers) below |

## Provider tiers

Not every provider carries the same support guarantees:

- **Enterprise-certified** — Claude (Agent SDK), Codex, AWS Bedrock, and Azure OpenAI. Stable model catalogues, compliance posture, and the integrations enterprise deployments depend on. **Use one of these for production / regulated workloads.**
- **Community / self-host tier** — OpenCode and other self-hosted/OpenAI-compatible runtimes. Fully supported for self-hosting and evaluation, but **not enterprise-certified**: OpenCode in particular resolves its model list from the remote `models.dev` registry, so individual models (including "free" ones such as the former `opencode/sonic`) can be removed without notice. There is no hardcoded default — you must pass an explicit `opencode:<provider/model>` or set `OPENCODE_DEFAULT_MODEL`; otherwise the build fails fast with an actionable error rather than silently using a dead model.

### OpenCode in the build sandbox — model catalogue caveat (#291)

OpenCode reads its model catalogue from `$XDG_CACHE_HOME/opencode/models.json` (falling back to `~/.cache/opencode/models.json`) and refreshes it from the remote `models.dev` registry at startup. AIFactory builds run inside an **OS sandbox that blocks that egress**, so a build cannot fetch the catalogue itself; with no usable catalogue OpenCode falls back to a small list compiled into the binary that omits newer models (e.g. `claude-sonnet-4-5`), and **every model fails with a fatal `ProviderModelNotFoundError`**.

AIFactory works around this automatically: before launching `opencode run`, the provider copies a previously-warmed catalogue into the cache path OpenCode reads — **plus** the `version` sentinel next to it, because OpenCode `rm -rf`'s its entire cache directory on startup whenever that sentinel does not match its baked-in cache version (which would otherwise wipe the catalogue we just injected).

**Limitation:** the workaround needs a warm catalogue to copy *from*. That file is populated by **any prior interactive `opencode` run on the host**. If OpenCode has never been run interactively on the build host (so no `~/.cache/opencode/models.json` exists), there is nothing to pre-warm and builds for models outside OpenCode's embedded fallback will still fail. To prime it once, run any `opencode run --model <provider/model> ...` interactively (with network access) on the build host before relying on OpenCode for sandboxed builds. This is one more reason OpenCode is **self-host / OSS tier only** — prefer Claude, Codex, Bedrock, or Azure OpenAI for production.

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

- `sonnet`, `opus`, `haiku`, `claude-*` → Claude Agent SDK
- `ollama:<model>` → Ollama
- `copilot:<backend>` → GitHub Copilot CLI (checked before the `claude-*`/`gpt-*` rules, since Copilot's own backend names are `claude-sonnet-4.5` / `gpt-5`)
- `gpt-*`, `*codex*` → Codex CLI
- `gemini-*` → Gemini CLI
- `<endpoint>:<model>` (with custom endpoint registered in Settings → LLM Providers) → OpenAI-compatible

## Where to configure

- **Per task** — Task Creation Wizard → Agent Profile dropdown
- **Per profile** — Settings → Agent Profile (create reusable profiles)
- **Per endpoint** — Settings → LLM Providers (register your endpoints, API keys are encrypted at rest)

## Local models: sizing & hardware

Local (Ollama) coding is **free and offline**, but unlike a one-shot chat it has to drive a
multi-step *agentic loop*: read files, call tools, write code, react to results. That asks far
more of a model than autocomplete, and model size matters a lot. From our
[provider benchmark](../showcase/benchmark-results):

| Model class | What to expect on a real multi-file task |
|---|---|
| **≤ 7B** | Not recommended — rarely sustains the tool-calling loop. |
| **14B** (e.g. `qwen2.5-coder:14b`) | Now *produces real code* (after the small-model fix below), but typically can't finish a whole multi-file feature — good for single-file edits and smoke tests. |
| **27–32B** (e.g. `qwen3-coder`, `qwen2.5-coder:32b`, `deepseek-coder-v2`) | The realistic floor for completing full tasks locally. Slower than cloud, review more. |
| **70B+** | Best local quality, but needs serious hardware. |

**The small-model fix.** Small local models often emit a tool call as a ```` ```json {…}``` ````
*text* block instead of the native `tool_calls` field, and tend to loop on `Read` without ever
writing. AIFactory now parses those text-emitted tool calls and nudges the model to write once it
has read enough — so a 14B goes from *writing nothing* to *writing real code*. (Ported from the
sister [TFactory](https://github.com/olafkfreund/TFactory) project.)

**Hardware, rough guide** (4-bit quantized, with a 32K context window):

| Model size | VRAM (approx) | Example GPU |
|---|---|---|
| 14B | ~10–12 GB | RTX 4070/4080, or a 16 GB card |
| 27–32B | ~24–32 GB | RTX 3090/4090 (24 GB), or better |
| 70B | ~48 GB+ | A6000 / dual-24 GB / data-center cards |

Run Ollama on a **dedicated GPU box**, not your daily-driver desktop — a 27B model will pin the
GPU and can take down a desktop session. Keep some VRAM headroom for the context window.

## The rule we never break

Claude interactions **always** route through `apps/backend/core/client.py::create_client()`. Never raw `anthropic.Anthropic()`. This is enforced in code review and is the only way OAuth-token auth + MCP server integration + per-agent tool permissions all work together.
