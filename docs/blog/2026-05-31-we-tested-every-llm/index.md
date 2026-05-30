---
slug: we-tested-every-llm-provider
title: "We ran the same build through every LLM — what won, what broke, and where local models stand"
authors: [olaf]
tags: [ai-coding, benchmark, ollama, local-llm, providers]
description: AIFactory is provider-agnostic, so we put it to the test — the identical coding task through Claude, Codex, Gemini, GitHub Copilot, and local Ollama, with independently-verified tests. Here are the real results, the bugs we fixed along the way, and an honest take on local vs cloud.
---

AIFactory claims to be provider-agnostic: the same build task should run on Claude, Codex,
Gemini, GitHub Copilot, or a local Ollama model. A claim like that is worth nothing until you
test it — so we did, with the *same* task, on *every* provider, and we re-ran the tests
ourselves rather than trusting the agent's word.

The short version: **every managed provider produced a working, tested feature** — and the
process surfaced (and fixed) four real bugs. Local models are a different story, and that story
is the interesting one.

{/* truncate */}

## The test

One small but non-trivial, uniformly testable feature, added to the same FastAPI demo repo by
every provider:

> A `tictactoe` module (3×3 board, win/draw detection, invalid-move handling), exposed via a
> `POST /tictactoe/move` endpoint, plus a pytest suite covering a win, a draw, and a rejected move.

Python + pytest on purpose: "result = working" is **objectively verifiable** by running the
tests, and the bar is identical for everyone. Each provider got the same prompt, ran in its own
isolated git worktree, and stopped at the human-review gate. Then we re-ran `pytest` on each
branch in a clean virtualenv — these are not self-reported numbers.

## The results

| Provider | Model | Time | Tests (independent `pytest`) |
|---|---|---|---|
| **Codex** | `gpt-5.3-codex` | **4m 56s** | ✅ 7 / 7 pass |
| **Claude** | `claude-sonnet-4-6` | 9m 29s | ✅ 9 / 9 pass |
| **Gemini** | `gemini-3.1-pro-preview` | 19m 37s | ✅ 16 / 16 pass |
| **Copilot** | `copilot:claude-sonnet-4.5` | ~25m | ✅ 4 / 4 pass |
| Ollama (local) | `qwen2.5-coder:14b` | ~3m | ⚠️ partial — code, not a finished feature |

- **Fastest working build: Codex.** Roughly twice as fast as the rest, with passing tests.
- **Cleanest first pass: Claude.** Followed the spec exactly, 9/9 on the first run, no fixes.
- **Most thorough: Gemini.** Wrote the largest suite, including idiomatic async API tests.
- **Most convenient: Copilot.** Runs on an existing GitHub Copilot subscription, no extra key —
  it's a router over Claude/GPT-5 backends. Also the slowest.

## The four bugs the benchmark found (and we fixed)

Running every provider through the *same* pipeline is a great way to find provider bugs:

1. **Codex wrote nothing** on the first run. Root cause: the agentic provider drained only
   stdout while the Codex MCP server filled its stderr pipe buffer and blocked. Fixed by draining
   stderr concurrently. The after-fix run is the fastest in the table.
2. **Gemini's code was correct but its tests "failed".** It wrote idiomatic async tests, but the
   repo had no `pytest-asyncio` configured. Fixed by configuring async test support — 8/10 became
   16/16 on a fresh run.
3. **Copilot wasn't supported at all** — so we added a provider for it.
4. **Ollama wrote nothing** — the most interesting one. More below.

## Ollama: what works, what needs doing

Local coding is fundamentally harder than a cloud chat. It isn't autocomplete — the model has to
drive a multi-step *agentic loop*: read files, call tools, write code, react to results. That
asks a lot of a model.

**What we fixed.** Small local models (qwen, llama) frequently describe a tool call as text — a
JSON blob in the message body — instead of using the native tool-call field, and they tend to
loop on reading without ever writing. AIFactory now parses those text-emitted tool calls and
nudges the model to write once it has read enough. With that fix, `qwen2.5-coder:14b` went from
**writing zero files** to **writing real code**. (We ported the fix from our sister project,
[TFactory](https://github.com/olafkfreund/TFactory), which had already solved it.)

**What still needs doing.** A 14B model now *starts* the job but usually can't *finish* a full
multi-file feature — in our run it produced a partial module and stopped after the first subtask.
That's not an AIFactory limit; it's a model-capacity limit.

### Which Ollama models, and what hardware

| Model class | What to expect on a real multi-file task |
|---|---|
| ≤ 7B | Not recommended — rarely sustains the tool-calling loop. |
| 14B (`qwen2.5-coder:14b`) | Produces real code; good for single-file edits and smoke tests; usually won't finish a whole feature. |
| **27–32B** (`qwen3-coder`, `qwen2.5-coder:32b`, `deepseek-coder-v2`) | The realistic floor for completing full tasks locally. |
| 70B+ | Best local quality, needs serious hardware. |

Rough hardware guide (4-bit quantized, 32K context):

| Model size | VRAM (approx) | Example GPU |
|---|---|---|
| 14B | ~10–12 GB | RTX 4070/4080 |
| 27–32B | ~24–32 GB | RTX 3090/4090 (24 GB) or better |
| 70B | ~48 GB+ | A6000 / dual-24 GB / data-center |

One hard-won tip: run Ollama on a **dedicated GPU box**, not your daily-driver desktop. While
testing, a 27B model pinned the GPU hard enough to take down a desktop session. Keep VRAM
headroom for the context window.

## Why the cloud models are still king

If you want one honest sentence: **for almost any real task today, a managed/cloud model is the
right default, and local models are the special case** — not the other way around.

- **Capability.** The frontier cloud models are far larger than anything you'll run on a single
  GPU. They sustain the agentic loop, follow multi-file instructions, and write passing tests on
  the first try. Three of four managed providers did exactly that here; the local 14B couldn't.
- **Speed.** Codex finished in under five minutes. A capable *local* model that can actually
  finish the task (27B+) runs several times slower on consumer hardware.
- **Cost, in context.** Cloud runs of a task this size cost on the order of a few cents. "Free"
  local coding costs $0 in API fees but needs a real GPU and your patience — and below ~27B it
  often can't finish at all.

**So when is local the right call?** When you *can't* send code to a third party — air-gapped,
regulated, or privacy-mandated environments. (That's the whole reason AIFactory is self-hostable;
see [Why we can't use Cursor at a bank](/blog/why-we-cant-use-cursor-at-a-bank).) In that case:
use a **27B+ coder model on a 24 GB+ GPU**, expect to review more, and lean on AIFactory's
human-review gate. For everyone else, pick a managed provider and move fast — AIFactory lets you
switch with a single model string, and even mix them per phase.

## See it / run it yourself

- The full, independently-verified results: **[Provider Benchmark](/showcase/benchmark-results)**.
- Every provider run is reproducible with `scripts/benchmark-provider.mjs` in the repo — re-run
  `pytest` on each branch and check our numbers.

Provider-agnostic isn't a marketing line if you can prove it. Now you can.
