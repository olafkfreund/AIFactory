---
title: Remote Control
sidebar_position: 4
---

# Remote Control — drive AIFactory agents from anywhere

> *"Start a task at your desk, then pick it up from your phone on the couch or a browser on another computer."* — [Anthropic's Claude Code docs](https://code.claude.com/docs/en/remote-control)

Remote Control wires Claude Code's [native `--remote-control` flag](https://code.claude.com/docs/en/remote-control) into AIFactory's agent spawn. When enabled per task (or per project), the running agent registers a session with Anthropic's API. You drive it from **claude.ai/code** in any browser or the **Claude mobile app**. The conversation stays in sync — type from the AIFactory portal, from your phone, from a laptop browser, all the same conversation.

The session runs locally (or on your AIFactory pod) the entire time. Outbound HTTPS only — no inbound ports. Survives network drops + laptop sleep.

## The user journey

1. **Enable Remote Control on a task** — toggle the checkbox in the Task Creation Wizard, OR turn on **Remote Control by default** in Project Settings
2. **Start the task** — AIFactory's `agent_service` spawns `claude --remote-control "AIFactory: <spec-id>"`
3. **Open [claude.ai/code](https://claude.ai/code)** from any device — the AIFactory session appears in the session list with a green status dot
4. **Click it** — you're now driving the same Claude Code conversation that's running in AIFactory. Approve permission prompts, type clarifying questions, hand off

The "Drive remotely ↗" badge in the task detail panel links you straight to claude.ai/code.

## Requirements

- **Claude Code v2.1.51 or later** on the AIFactory host (we ship v2.1.150+ in the chart's image)
- **Anthropic subscription**: Pro / Max / Team / Enterprise. API-key auth does NOT work — Remote Control routes through Anthropic's session infrastructure, which requires claude.ai OAuth.
- **Full-scope `claude auth login` credentials** on the host. The OAuth token AIFactory uses by default (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`) is inference-only and gets rejected by Remote Control. AIFactory's `agent_service` handles this automatically — when Remote Control is enabled for a task, it scrubs the env var so the subprocess falls back to `~/.claude/.credentials.json`.
- For Team / Enterprise plans: an org admin must enable Remote Control in [claude.ai/admin-settings/claude-code](https://claude.ai/admin-settings/claude-code).

## How auth works

The `claude` subprocess looks for credentials in this order (`apps/backend/core/auth.py::get_auth_token`):

1. `CLAUDE_CODE_OAUTH_TOKEN` env var — **inference-only, gets rejected by Remote Control**
2. `ANTHROPIC_AUTH_TOKEN` env var — also limited
3. AIFactory profiles (`~/.aifactory/claude-profiles.json`)
4. **`~/.claude/.credentials.json`** — produced by `claude auth login`, full-scope ✓

When a task has `enableRemoteControl: true`, `agent_service` removes vars (1) and (2) from the spawned subprocess's env, forcing the chain through to (4). The operator must therefore make sure (4) exists on the AIFactory host.

## Local AIFactory deployment

If you run AIFactory on your laptop, `~/.claude/.credentials.json` is already there (you ran `claude` at least once). Just toggle Remote Control on a task and go.

## K8s / VPS deployment

The AIFactory pod's home directory is `/home/nonroot/`, where there's no `~/.claude/.credentials.json` by default. You mount one from a Kubernetes Secret.

### One-time setup

```bash
# 1. On a machine where you've signed in via `claude auth login`:
kubectl create secret generic claude-remote-credentials \
  --from-file=credentials.json=$HOME/.claude/.credentials.json \
  -n aifactory

# 2. In your Helm values.yaml:
remoteControl:
  enabled: true
  credentialsSecretName: claude-remote-credentials

# 3. Upgrade the chart:
helm upgrade aifactory ./charts/aifactory -n aifactory -f values.yaml
```

The chart mounts the secret at `/home/nonroot/.claude/.credentials.json` with `subPath: credentials.json`, `readOnly: true`, and `defaultMode: 0400` (Claude Code refuses to read credentials with looser perms).

### Rotating credentials

```bash
# After re-running `claude auth login`:
kubectl create secret generic claude-remote-credentials \
  --from-file=credentials.json=$HOME/.claude/.credentials.json \
  -n aifactory \
  --dry-run=client -o yaml | kubectl apply -f -

# Restart the pod so the volume picks up the new file
kubectl rollout restart deployment/aifactory -n aifactory
```

Recommended cadence: monthly. Anthropic's session tokens are long-lived but compromise risk grows with time.

### Security posture

The credentials file in the Secret is full-scope — anyone holding it can use your Anthropic subscription. The chart minimizes exposure:

- `readOnly: true` on the volume mount
- `defaultMode: 0400` on the Secret volume (file-mode 0400; only the pod user reads it)
- `subPath: credentials.json` so the file lands at exactly the path Claude Code expects, not in a directory of projected secrets
- The Secret is namespace-scoped — RBAC the namespace tightly

Anti-patterns to avoid:

- ❌ Baking credentials into the container image
- ❌ Storing in plain `values.yaml` and checking that into git
- ❌ Sharing across multiple AIFactory installs (rotate per install)
- ❌ Using a personal account's credentials for shared deployments — use a dedicated service account

## Limits (from Anthropic's docs)

- **One Remote Control session per Claude Code process.** AIFactory already runs one Claude per task — fine. If you try to also `claude --remote-control` from another local shell, Claude rejects the second connection.
- **Provider lock-in**: Remote Control requires the Anthropic API. Bedrock / Vertex / Foundry providers can't use Remote Control. (AIFactory's multi-provider routing is untouched by this — non-Anthropic phases of a task run as before.)
- **Some commands are local-only**: `/mcp`, `/plugin`, `/resume` work only in the local CLI. `/compact`, `/clear`, `/context`, `/usage` work from mobile + web.
- **Extended network outage**: if the AIFactory host is awake but unreachable for ~10 min, the session times out. Restart the task to get a new session.

## What's NOT this feature

A separate parallel feature, tracked at [#83](https://github.com/olafkfreund/AIFactory/issues/83), exposes AIFactory as an **HTTP+SSE MCP server** for non-Claude-Code clients (Cursor, Continue.dev, programmatic scripts). Use that path if you want to drive AIFactory from a non-Claude AI client; use this Remote Control feature for the Claude-native experience.

## Troubleshooting

### "Remote Control requires a full-scope login token"

The pod's `CLAUDE_CODE_OAUTH_TOKEN` is leaking past the agent_service's scrub OR `~/.claude/.credentials.json` is missing. Check:

```bash
kubectl exec -n aifactory deploy/aifactory -- ls -la /home/nonroot/.claude/
# Should show: -r-------- credentials.json
```

If missing → your Secret didn't mount; re-check the `credentialsSecretName` value.

### "Remote Control is not yet enabled for your account" or "...disabled by your organization's policy"

The Anthropic-side gate. Team / Enterprise needs an admin toggle. See [Anthropic's troubleshooting](https://code.claude.com/docs/en/remote-control#troubleshooting).

### Session doesn't appear in claude.ai/code

Wait ~30 s after the task starts (session registration is async). Then refresh claude.ai/code. Also verify the spawn happened:

```bash
kubectl logs -n aifactory deploy/aifactory | grep "Remote Control ENABLED"
```

You should see:

> `[AgentService] Remote Control ENABLED for task_id=... — session 'AIFactory: <spec-id>' will appear in claude.ai/code.`

If that log line is missing, the per-task toggle didn't flow through — check `task_metadata.json` in the spec dir.
