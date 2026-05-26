---
title: Troubleshooting
sidebar_position: 2
---

# Troubleshooting

The eight issues we see most often, with diagnostics and fixes.

## `Stream closed` errors when starting a task

**Symptom:** the task starts then immediately stops with "Stream closed" in the logs.

**Cause:** the Claude Agent SDK expects `permission_mode="bypassPermissions"` when running headless.

**Fix:** ensure `ClaudeAgentOptions` sets `permission_mode="bypassPermissions"` in every code path, and that `APP_TOOLS` in `apps/web-server/server/services/models.py` lists every MCP tool the agent might call.

## Task stuck in "Planning" forever

**Symptom:** the kanban card sits at 100% planning, never progresses.

**Cause:** the agent process is alive but its `build-progress.txt` isn't being flushed.

**Fix:** check `<project>/.aifactory/worktrees/tasks/<spec-id>/.aifactory/specs/<spec-id>/build-progress.txt`. The web-server reads this via the 3-second sync loop (`agent_service.py::_sync_worktree_files`). If the file is empty, the agent is hung — click **Stop** and inspect the agent's stderr in the system logs.

## Live Console shows "Connecting..." forever

**Symptom:** the Live Console tab shows the spinner indefinitely.

**Known causes** (each fixed in PR #69, but listing for prod-debug reference):

1. **WS URL missing auth token** — use `getAuthenticatedWsUrl()` helper, not raw URL concat
2. **`/var/run/aifactory/panes` not writable** — fall back to `$XDG_RUNTIME_DIR/aifactory-rmux/panes`
3. **Stale rmux session** from a previous web-server lifetime — the session registry now pre-emptively kills with `ignore_missing=True`
4. **Vite proxy missing `ws: true`** — check `apps/frontend-web/vite.config.ts`

## "Project not found" after registering

**Symptom:** POST `/api/projects` returns 201, but subsequent calls return 404.

**Cause:** the project list is cached in `~/.aifactory/projects.json`. If the file is read-only or owned by a different uid, writes silently fail.

**Fix:** `ls -la ~/.aifactory/projects.json` — chown to your user, retry.

## GitLab/Azure DevOps PR import returns wrong provider name

**Symptom:** PR loading state shows "GitHub" for a GitLab PR.

**Fix:** this was fixed in commit `00a6463`. Pull latest dev.

## Gemini sunset

Gemini CLI hits end-of-life **2026-06-18**. Migrate to either:

- **Gemini via Google AI Studio API** (set up via Settings → LLM Providers → "Add OpenAI-compatible endpoint" with `https://generativelanguage.googleapis.com/v1beta/openai`)
- **Anthropic Claude** for planning + QA (most reliable path)

## E2E fixtures

**Symptom:** Playwright tests fail with `expect(listRes.status()).toBe(200); Received: 404`.

**Cause:** the E2E fixture expects a real project to exist on disk. CI doesn't seed one by default.

**Fix:** the workflow needs to `mkdir -p /tmp/aifactory-e2e-fixture` before booting the web-server, and set `AIFACTORY_TEST_AGENT_CMD="sleep 300"`. See `.github/workflows/ci.yml` for the working pattern.

## Database migration crash on upgrade

**Symptom:** web-server fails to start after `git pull` with "table X has no column Y".

**Fix:** Alembic migration didn't run. From `apps/web-server/`:

```bash
alembic upgrade head
```

The web-server auto-runs this at boot — if it skipped, check for a stuck advisory lock (rare; happens if a previous boot was SIGKILL'd mid-migration).
