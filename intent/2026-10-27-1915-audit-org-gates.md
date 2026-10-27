---
status: approved
issue: 1915
author: olafkfreund
---

# Intent: access-review and audit-export org gates

Closes #1915. Refs olafkfreund/Factory#312.

## Problem

1. `GET /api/admin/access-review` authorizes `?org_id` and streams `?org`, so
   `?org_id=A&org=B` lets an admin of A download B's member roster.
2. `GET /api/audit/export` has no org gate. Any authenticated user can stream
   any org's audit log, or the whole deployment's log when `org_id` is left out.

## Proposed outcome

- The access review streams only the org the caller was authorized for (one
  `org_id` param).
- The audit export with `org_id` requires the `admin` role in that org.
  Without `org_id` it is refused unless the caller is a service principal.

## Affected users and systems

AIFactory web-server `routes/access_review.py` and `routes/audit.py`. Org
admins and auditors. No code caller of either endpoint exists in any Factory
repo or in factory-gitops (checked).

## Constraints

No gitops changes. The service principal (legacy token, auth-disabled dev)
keeps its unscoped export. Do not trust any JWT role claim (#1909).

## Open questions

None. The same audit-export gap in PFactory and TFactory is tracked in PF #941
and TF #1451.
