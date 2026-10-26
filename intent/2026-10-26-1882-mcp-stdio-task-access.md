---
status: approved
issue: 1882
author: olafkfreund
---

# Intent: per-task access check on the mcp-stdio write proxies (#1882)

## Problem

The stdio-MCP write proxies in `apps/web-server/server/mcp_stdio/router.py`
authorise only with `Depends(require_acw_scope(<scope>))` and then call the
REST handler as a plain function, so the REST route's own
`require_task_access("member")` / `require_project_access("member")`
dependency never runs:

| Proxy (router.py) | Delegates to | REST gate skipped |
| --- | --- | --- |
| create-and-run :213 | `routes/execution.py:1181` | `require_project_access("member")` :1187 |
| start :243 | `routes/execution.py:288` | `require_task_access("member")` :292 |
| stop :263 | `routes/execution.py:961` | `require_task_access("member")` :963 |
| recover :282 | `routes/execution.py:1021` | `require_task_access("member")` :1024 |
| approve-plan :302 | `routes/plan_approval.py:57` | `require_task_access("member")` :60 |
| create-pr :327 | `routes/pr.py:99` | `require_task_access("member")` :102 |
| merge :349 | `routes/worktree_merge.py:1833` | `require_task_access("member")` :1836 |

`TokenAuthMiddleware` lets `/api/mcp-stdio/` through unauthenticated and sets
`request.state.user = None` (`server/auth.py:268`, `:275`), so nothing else
checks it either. Any `acw_` key with `task:write` (or `task:merge`) can act on
any task id it names, in any organisation.

## Facts: how acw keys are bound

- **Model.** `ApiKey` has `user_id` and `org_id`, both non-null foreign keys
  (`server/database/models.py:386-391`). Its docstring: "scoped to a user and
  organization" (:379).
- **Mint.** `POST /api/api-keys` (`routes/api_keys.py:127-174`) requires a
  logged-in user, checks that user is an `OrgMember` of `body.org_id` (:143-154)
  and stores `user_id=current_user.id, org_id=body.org_id` (:172-174).
- **Resolve.** `mcp_remote.auth._lookup_by_digest` returns
  `AuthenticatedKey(key_id, scopes, user_id, org_id)` from the row
  (`mcp_remote/auth.py:115-135`). `require_acw_scope`
  (`mcp_stdio/auth.py:87-141`) checks only that the key exists, is active and
  has the scope.
- **REST parity.** The same `acw_` key on the normal REST surface becomes the
  principal `{"id": key.user_id, "role": "user", "org_id": ..., "is_service":
  key.user_id is None}` (`server/auth.py:192-216`, used at :361-372), and the
  REST routes then run `require_task_access`. So the key is already
  user-bound by design; only the stdio proxy skips the check.
- **Legacy token.** The `API_TOKEN` wildcard maps to `_LegacyAdminKey`
  (`mcp_stdio/auth.py:59-78`) on the proxy and to an `is_service` principal on
  REST (`server/auth.py:349-355`). Service principals bypass per-org checks by
  design (`routes/project_authz.py:47-57`).

## Facts: what `require_task_access` checks

`require_task_access(role)` → `TaskAccessChecker` (`routes/project_authz.py:242-262`):
takes the project id from `task_id` (`project_id:spec_id` → part before `:`),
then `authorize_project_for_user` (:121-171): auth disabled → allow; service
principal → allow; else load the project, look up `OrgMember(org_id=project.org_id,
user_id=user.id)` and apply `check_project_access` (:59-113): missing project
→ 404, project without `org_id` → 403, not a member → 403, role below
`role` → 403. 403s write an `authz.denied` / `gate.rejected` audit row.

## Proposed outcome

An `acw_` key on any stdio-MCP write proxy gets the same answer the REST route
would give that key: 403/404 for a task or project outside the key owner's
organisations, success for the owner's own tasks. The legacy admin token keeps
working as a service principal.

## Affected users and systems

AIFactory web-server (`mcp_stdio` proxy). Users of the `aifactory` stdio MCP
server with a minted `acw_` key. Single-user installs on the legacy token are
unaffected.

## Constraints

- Reuse the existing rule (`authorize_project_for_user`); no new authz rules.
- Keep the 401 / 403 split of `require_acw_scope`.
- The apply-correction proxy from #1881 is not on `dev` yet; it needs the same
  check once it lands.

## Open questions

None. Lead's decision rule: keys carry a user and org binding, so enforce.
