---
status: approved
issue: 1960
author: olafkfreund
---

# Intent: record the originator of a task at intake

Epic: olafkfreund/Factory#1960. Parent intent: Factory `intent/2026-10-26-1960-agent-git-identity.md` (merged in Factory #3661), slice 2 ("Stage 3: capture the originator at intake"). Slice 1 shipped in AIFactory #1887 (`ApiKey.agent_identity`).

## Problem

A task records what to build but not who asked for it. The MCP proxy (`mcp_stdio/router.py` `proxy_create_and_run_task`) already resolves the `acw_` key and its owner, then drops both. REST/CLI task creation has the principal on `request.state.user` and drops it too. From-issue intake fetches the issue through the provider, whose `IssueData.author` is discarded by `_fetch_issue_via_provider`. Later slices (git trailers, run signature `originator`) have nothing true to write.

## Proposed outcome

- Every task created through MCP, the REST/CLI create routes, or from-issue intake stores `originator` on the spec: the authenticated user's id (or email when the principal has one), or `<provider>:<login>` (e.g. `github:octocat`) for an issue-created task.
- An `acw_`-created task also stores `agent_identity` (`agent:aifactory/<key_id>`, from #1887), separate from `originator`.
- When either is unknown (legacy admin token, service principal, auth disabled, issue without author) it is stored as null, never guessed.
- `GET /api/tasks/{id}` (and the MCP `task_get` proxy) returns both.

## Remaining slices of the epic, in order

1. This slice: originator at intake (MCP/CLI, then from-issue).
2. Git trailers on branch commits (`Agent-Identity:`, `Originator:`), behind a default-off flag.
3. Factory GitHub App opens and merges agent PRs (needs the owner to create the App; AIFactory #1701).
4. Required reviews in branch protection on the four repos.
5. Revocation and expiry for agent identities; 6b: lock `_SAFE_GIT_KEYS`.
6. Hard gate before the ceiling reaches the merge path: unceiled keys default to `hard` or are refused.
7. Portal intake originator: last or never, pending the owner's auth decision.

## Affected users and systems

AIFactory web-server: task creation routes, MCP proxy, from-issue route, task read model. PFactory/TFactory unaffected. Tasks are spec directories on disk, so no database migration.

## Constraints

- Additive only: no change to authentication, scopes or access checks; no lockout, no credential change.
- No git trailers in this slice.
- No placeholder identities: unknown is null.
- No request field sets `originator` directly. It comes from the authenticated principal, or from the issue's author (provider-fetched, or in the issue payload the caller is already trusted to supply for title, body and labels).

## Open questions

None.
