# Scoped MCP API keys (stdio)

When you use AIFactory through Claude Code, a small **stdio MCP server** runs
on your laptop as a subprocess. It calls into the AIFactory web-server to
list tasks, start runs, create PRs, and so on. Before v1.1 the stdio MCP
used the **host-wide admin token** at `~/.aifactory/.token` — fine for a
single user, dangerous on shared hosts because any process that could
read that file got full admin power.

This page covers the v1.1 model (Issue #154): each developer holds their
own **scoped `acw_` key** for the stdio MCP, the legacy admin token
keeps working as a fallback.

## Why you'd use this

You need this if **either** is true:

- Multiple humans share a machine that runs AIFactory.
- A CI runner / shared Kubernetes node hosts the stdio MCP.

If you're the only user on your own laptop, the legacy admin token still
works — there's nothing to do.

## Mint a key

1. Open AIFactory → **Settings → API Keys**.
2. Click **Mint key**.
3. Pick a **name** (e.g. `laptop-ada`) and the **scopes** you actually need.
4. Optional: set an expiry in days.
5. Hit **Mint key**. The full `acw_…` string is shown **once** —
   copy it before closing the panel.

## Scopes

| Scope            | What it gates                                                        |
| ---------------- | -------------------------------------------------------------------- |
| `mcp:read`       | List + status + logs + worktree diff (all read-only tools)           |
| `project:write`  | Create new projects                                                  |
| `task:write`     | Start / stop / recover / approve / create-and-run tasks              |
| `task:merge`     | Create PRs and merge worktrees (highest blast radius — opt in)       |

Day-to-day workflows usually want `mcp:read` + `task:write`. Grant
`task:merge` only on machines that should be able to push to the
default branch.

## Drop the key on the laptop

The stdio MCP client checks these sources in order — the first
non-empty value wins:

1. `$AIFACTORY_MCP_KEY` env var
2. `~/.aifactory/.mcp-key` file
3. `$AIFACTORY_API_TOKEN_FILE` (legacy override)
4. `~/.aifactory/.token` (legacy admin — wildcard scopes)

The env-var form is recommended for shell-scoped usage:

```bash
export AIFACTORY_MCP_KEY="acw_..."  # paste the value once
```

For persistent use, drop it in `~/.aifactory/.mcp-key` (`chmod 600`).

## What gets recorded

Every call carries the bearer token. The web-server's audit log
records the **key id** (the database row, not the secret) so you can
see exactly which key did what. Calls that fall back to the legacy
admin token are logged with `key_id="legacy-admin"` so they stand
out for review.

## Revoking a key

Revoke a compromised or unused key from **Settings → API Keys** → trash
icon. Revocation is immediate — the next stdio MCP call with that
key returns 401 and the user sees:

> AIFactory MCP key rejected — mint a fresh key in Settings → API Keys.

If the user's key was simply *under-scoped* (they tried to merge a PR
with an `mcp:read`-only key), the proxy returns 403 with the missing
scope name so they know which scope to add when re-minting.

## Architecture

Under the hood, stdio MCP calls hit a proxy at `/api/mcp-stdio/*`
(separate from the regular REST surface). Each proxy route is gated
by `acw_` validation + an explicit scope check. The proxy delegates
to the same handler functions the regular REST routes use, so
behaviour is identical — only the auth posture differs.

See the [Remote Control MCP server](./mcp-servers.md) page for the
parallel `acw_` model used by the remote HTTP+SSE MCP surface.
