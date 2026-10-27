---
status: draft
issue: 1910
author: olafkfreund
---

# Intent: remote MCP tools authorize the calling key, not the service token

Closes #1910. Refs olafkfreund/Factory#312. Follow-up to #1885 (stdio) and #1914.

## Problem

`mcp_remote/tools.py::_call_internal` proxies every tool call to REST with the
legacy `settings.API_TOKEN`. REST sees a service principal, so
`project_authz` skips the org checks. `dispatch_tool_call` only checks the key's
scope. Since #1914, `acw_` keys authenticate, so a member of org A whose key has
`mcp:write` can start, stop, approve, reject, recover and merge tasks in any
org. The read tools (`list_projects`, `list_tasks`, `get_task`, diff, QA report)
also leak across orgs.

## Proposed outcome

- Every task or project tool authorizes the key's principal against the
  target project before it proxies: viewer for `mcp:read` tools, member for
  `mcp:write` tools. This is the check stdio runs (`require_acw_access`).
- `list_projects` returns only projects in orgs the key may see. A key is bound
  to `key.org_id`.
- Own-org calls work as before. Service keys (no owner) are unchanged.

## Affected users and systems

AIFactory web-server `mcp_remote/tools.py`. Only deployments with
`AIFACTORY_MCP_REMOTE_ENABLED` set. It is off by default and off in prod.

## Constraints

No schema or gitops change. Reuse `authorize_project_for_user`,
`accessible_org_ids` and `mcp_stdio.auth.acw_principal`. Do not widen what an
MCP key can do on REST.

## Open questions

None.
