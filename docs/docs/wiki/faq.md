---
title: FAQ
sidebar_position: 1
---

# FAQ

## Why does AIFactory exist when Cursor / Devin / Aider exist?

Different category. Cursor is a code editor with AI. Devin is a hosted agent service. Aider is a CLI pair-programmer. AIFactory is a **task management + agent orchestration platform** — it sits one level above the agent and treats it like a build runner.

Concretely, AIFactory is what you reach for when:

- You have a backlog of GitHub issues and you want to triage them into agent-shippable units without writing prompts
- You want **spec-first** development: a reviewable artifact before code is written
- You want to **mix providers** by phase (Claude planning, cheap local coding)
- You need **audit trail + SOC2 evidence** for AI-assisted work

## Does it work offline?

The portal does. The agent only works offline if you point it at a local Ollama instance — see [Multi-Provider](../concepts/multi-provider).

## How does authentication work?

Web-server auth: a token (rotating, printed to console at boot) that the frontend stores. Optionally OIDC SSO (Keycloak / Okta / Azure AD) via the `OIDC_ISSUER_URL` env var.

Claude auth: the OAuth token from `claude setup-token`. Never the direct API key.

## Can I run multiple tasks in parallel?

Yes. Each task runs in its own subprocess and its own worktree. Resource limits are per task; no shared mutable state.

## What if the agent gets stuck?

The runtime detects stuck subtasks (no log output for N minutes) and invokes the **Coder Recovery** agent (`prompts/coder_recovery.md`). After three failed recoveries, the task is paused and surfaced to the UI.

You can also click **Stop** at any time — that sends SIGTERM to the agent process and stops the rmux session if one exists.

## How do I rotate the OAuth token?

```bash
claude setup-token
# Replace CLAUDE_CODE_OAUTH_TOKEN in apps/backend/.env
# Restart the web-server
```

No tasks need to be cancelled — running tasks already hold a session.

## Where does my code go?

Nowhere it wasn't already. Agent edits land in a git worktree under `.aifactory/worktrees/tasks/<spec-id>/`. Nothing leaves your machine unless you push.

LLM providers see only the file contents the agent sends them — typically the files relevant to the task (chosen via the `codebase_analyzer` sub-agent) plus the spec.

## How does the CI fixture seeding work?

The E2E tests use `AIFACTORY_TEST_AGENT_CMD=sleep 300` to replace the agent subprocess with a no-op while the rmux integration hook still creates the live session. This lets the Playwright suite exercise the Live Console without burning LLM tokens. See [Troubleshooting](./troubleshooting#e2e-fixtures) for details.
