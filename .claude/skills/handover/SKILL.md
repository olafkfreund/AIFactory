---
name: handover
description: >
  Hand off the current task to AIFactory's autonomous pipeline so it runs in
  the background (planner → coder → QA → human-review) while the developer
  works on something else.
when_to_use: >
  Activate when the user says "/handover", "hand this off to AIFactory",
  "send this to AIFactory", "run this overnight", or otherwise wants to
  move a task they've been discussing interactively into AIFactory's
  autonomous build pipeline.
allowed-tools:
  - mcp__aifactory__task_create_and_run
  - mcp__aifactory__project_list
  - mcp__aifactory__project_create
  - mcp__aifactory__task_status
  - Bash(git config*)
  - Bash(git rev-parse*)
disable-model-invocation: false
user-invocable: true
---

# /handover — Send the current task to AIFactory

Move the task the user has been discussing in this Claude Code session into
AIFactory's autonomous build pipeline. AIFactory runs it through
planner → coder → QA → human-review without further input from the user
until the plan-review gate.

The underlying primitive is `mcp__aifactory__task_create_and_run` (shipped
in Epic #50 PR #114 — M2 stdio task-control tools). This skill is the
ergonomic shortcut: capture conversation context, infer a sensible task
spec, call the tool, surface the result.

## What to do

### 1. Determine the task description

- **If the user typed text after `/handover`**, treat that as the description
  override and use it directly.
- **Otherwise**, infer the description from the last 5–10 conversation turns:
  - Summarise what they're trying to build
  - List the relevant files / endpoints / constraints they mentioned
  - Capture any acceptance criteria they hinted at
- **If the conversation context is thin** (fewer than 3 substantive turns
  about the task) AND they didn't provide an override, **STOP and ask one
  clarifying question** rather than guess. Better to spend 30 seconds asking
  than burn an autonomous agent run on the wrong task.

### 2. Determine the AIFactory project

The current working directory points at a repo that should be registered in
AIFactory's project list. Call `mcp__aifactory__project_list` and pick the
project whose `path` matches (or starts with) the cwd.

#### If no project matches

Two options — pick based on what's actually available:

1. **Cwd is a git repo with a remote origin** (epic #82 PR-A path).
   `git rev-parse --is-inside-work-tree` succeeds AND
   `git config --get remote.origin.url` returns a value. Offer:

   > *"I don't see a matching project on the portal, but I see `<origin URL>` for this cwd. Should I register it so the portal clones it itself? [y/N]"*

   On yes, also grab the current branch (`git rev-parse --abbrev-ref HEAD`)
   and call `mcp__aifactory__project_create` with:

   - `git_url`: the origin URL
   - `branch`: the current branch
   - `name`: the repo basename
   - `confirm`: false first to surface the confirm-gate preview, then
     `confirm: true` to actually create.

   On portal-managed deployments (K8s/SaaS) this is the only viable path —
   the user's repo isn't on the portal's filesystem. On local laptop
   installs, this is also a perfectly fine path (just slower than option 2
   because of the clone). The clone lands in `PROJECT_WORKSPACE_ROOT`
   (defaults to `~/.aifactory/workspaces/`).

2. **Cwd is NOT a git repo OR has no origin remote**.
   Ask the user which project to use — don't guess.

Either way: once a project_id is in hand, proceed to step 3.

### 3. Compose the title

5–10 words summarising the work. This becomes the PR title when AIFactory
finishes. Examples:

- "Add JWT refresh tokens to auth middleware"
- "Refactor GitHub provider for GitLab MR auto-rebase"
- "Backfill tests for the rmux WebSocket bridge"

### 4. Compose the description

3–10 sentences. Include:

- **What** needs to be built (the headline)
- **Why** (1 sentence of motivation if not obvious)
- **Acceptance criteria** — specific files, behaviours, or tests the
  autonomous agent should produce
- **Context** the agent will need — related issue numbers, existing
  patterns in the codebase, constraints the user mentioned

### 5. Call the tool

`mcp__aifactory__task_create_and_run` with:

- `project_id`: from step 2
- `title`: from step 3
- `description`: from step 4
- `confirm: true` — `/handover` IS the explicit confirmation; the M2
  confirm-gate exists to stop autonomous LLMs from kicking off paid runs
  unprompted, which doesn't apply here because the human explicitly
  invoked this skill.

### 6. Report back

Format the response as:

```
✅ Handed off to AIFactory.

• Task: <task_id>
• Title: <title>
• Track at: https://aifactory.freundcloud.org.uk/tasks/<task_id>

AIFactory's planner runs first — when it hits the plan-review gate the
portal will show the implementation plan for your approval. You can also
poll status from here with mcp__aifactory__task_status.
```

If the tool returns an error, surface it verbatim. The MCP HTTP client
already emits operator-actionable single-line guidance:

- Web-server not reachable → `"AIFactory not reachable at https://aifactory.freundcloud.org.uk — check the deployment / network"`
- Token missing → `"AIFactory API token not found at ~/.aifactory/.token-deployed — set it from the deployment's APP_API_TOKEN"`
- Token rejected → `"AIFactory token at ~/.aifactory/.token-deployed rejected — it must match the deployment's APP_API_TOKEN (factory-secrets)"`

Don't transform or paraphrase these messages — the user knows what to do
with them.

## When to use this

✓ The task is bigger than the user wants to do interactively right now
✓ It's repetitive / boring work that doesn't need turn-by-turn input
✓ The user wants to step away (overnight, lunch, focus block) and come back to a draft PR
✓ A junior task they'd otherwise pass to a less-senior teammate

## When NOT to use this

✗ The user is debugging — interactive iteration is faster than autonomous loops
✗ The task needs creative judgement on every step
✗ There's no clear definition of done — fix that first, then `/handover`
✗ The AIFactory deployment (https://aifactory.freundcloud.org.uk) is unreachable — check the deployment / network before retrying

## User's optional override

$ARGUMENTS
