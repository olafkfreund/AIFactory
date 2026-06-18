---
title: Solo mode
sidebar_position: 6
---

# Solo mode

> *One self-directed agent that writes its own plan and works it — skipping the planner, plan-review gate, and QA loop. A token-saving path for small jobs. Off by default.*

The default AIFactory pipeline runs four roles in separate sessions: **planner → coder → QA reviewer → QA fixer**. That structure earns its keep on real features — but for a one-line fix or a throwaway script it's overhead: four sessions, several context loads, and a plan-review gate for work that doesn't need one.

**Solo mode** collapses the pipeline into a single agent. That one agent:

- writes its own `implementation_plan.json`,
- implements the subtasks **in the same loop**,
- skips the dedicated planner session, the plan-review gate, and the QA validation loop.

The result is far fewer tokens and sessions for small jobs. The trade-off is no independent QA pass — so it's **opt-in** and best reserved for low-risk, small-scope tasks. For anything you'd want reviewed, leave it off and use the full pipeline.

## Enabling it

Solo mode resolves through the same channels as other opt-in features, in this order (first match wins):

1. **`AIFACTORY_SOLO_MODE`** environment variable — `1/true/yes/on` or `0/false/no/off`.
2. **Per-task** — the `soloMode` flag in the task's `task_metadata.json`, set from the web UI (Task Creation Wizard / settings toggle).
3. **Global** — `solo.enabled` in `~/.aifactory/config.json`.
4. **Default** — disabled (backward compatible).

The environment variable always wins, so you can force solo mode on (or off) for a whole host regardless of per-task or global settings.

## When to use it

- Good fit — small, low-risk jobs: a one-file fix, a quick script, a smoke-test spec.
- Good fit — token-sensitive runs where the full four-session pipeline is overkill.
- Not a fit — anything you want an independent QA pass on; keep the default pipeline.
