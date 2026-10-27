---
status: draft
issue: 1915
intent: intent/2026-10-27-1915-audit-org-gates.md
---

# Spec: access-review and audit-export org gates

## Design

1. `routes/access_review.py::export_access_review`: drop the separate
   `org` query param. Stream `membership.org_id`, the org that
   `require_org_role("admin")` resolved from `?org_id`, and use it in the
   filename. One param means the gate and the data cannot disagree. Update the
   AIF docs that show `?org=`: `docs/docs/concepts/audit-anchor.md`.
   factory-gitops docs are off limits this wave; this is noted in the PR.
2. `routes/audit.py::export_audit_logs`: add `request: Request`.
   - When `org_id` is set, look up the caller's `OrgMember` in it. A missing
     membership or `_role_level(role) < _role_level("admin")` gives 403.
   - When `org_id` is None, return 403 unless
     `is_service_principal(request.state.user)` (from `project_authz`) or
     auth is disabled.
   - Update the docstring.

## Alternatives rejected

- Keep `org` and also reject a mismatch with `org_id`. That keeps two names
  for one thing, and `?org=` alone never worked anyway (422).
- Let any member export their own org. Audit logs are admin evidence, and the
  per-org `/api/orgs/{org_id}/audit` list route is the member surface.

## Risks

A human who relied on the unscoped export loses it. No code caller exists, and
the service principal keeps it.

## Verification

New `tests/test_audit_org_gates.py` (HTTP client, SQLite). These fail on dev:
- access review with `?org_id=A&org=B` by an admin of A returns only A's roster;
- export by a member (not admin) of the org gives 403;
- export by a non-member gives 403;
- unscoped export by a human gives 403.

These pass both before and after:
- export by an admin of the org gives 200, with only that org's rows;
- unscoped export by a service principal gives 200.

Run `tests/audit`, plus the ratchets.
