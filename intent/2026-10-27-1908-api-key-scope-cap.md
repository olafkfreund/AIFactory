---
status: approved
issue: 1908
author: olafkfreund
---

# Intent: cap acw_ key scopes by org role, and stop trusting a JWT role claim

Closes #1908 and #1909. Refs olafkfreund/Factory#312 (RBAC review, Factory PR #3663).

## Problem

1. `POST /api/keys` checks org membership only, so a `viewer` can mint an
   `acw_` key with write scopes. Remote MCP (opt-in) gates its write tools
   (start/stop/approve/reject/recover/merge) on the key's `mcp:write` scope
   alone, so a viewer with such a key gets full write. REST and stdio MCP are
   already capped by the owner's role (#1885), as verified on dev.
2. `is_service_principal()` returns true for any principal with
   `role == "admin"`. Human JWTs carry `User.role`, which OIDC provisioning
   sets from `APP_OIDC_GROUP_TO_ROLE` / `APP_OIDC_DEFAULT_ROLE` (an org
   role). If a group is mapped to `admin`, those users bypass every org check
   in every org. This is latent: the current deployment sets neither variable.

## Proposed outcome

- Below `member`, a caller can only request read scopes (`mcp:read`). Any
  other requested scope gets a 403. Roles at `member` and above behave as
  they do today.
- `is_service_principal` is true only for `is_service`. Auth-disabled dev
  mode keeps working because its principal is marked `is_service` explicitly.

## Affected users and systems

AIFactory web-server: `routes/api_keys.py`, `routes/project_authz.py`,
`auth.py` (DISABLE_AUTH principal). Viewers minting keys. Deployments that
map OIDC groups to `admin`.

## Constraints

- Must not lock out any login or revoke existing keys, and must not touch
  gitops or the cluster.
- No new roles or scope constants. Use `ROLE_LEVELS` and the existing
  `mcp:read`.

## Open questions

None. Out of scope: existing viewer keys that already carry write scopes are
not revoked. Remote MCP's missing org/project check is filed as #1910.
