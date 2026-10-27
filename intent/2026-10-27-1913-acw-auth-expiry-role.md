---
status: approved
issue: 1913
author: olafkfreund
---

# Intent: acw_ auth enforces expiry and the owner's current org role

Closes #1913. Refs olafkfreund/Factory#312. Follow-up to #1908 (PR #1911).

## Problem

Every `acw_` path (REST middleware Strategy 3, stdio MCP, remote MCP)
authenticates through `mcp_remote/auth.py::_lookup_by_digest`. It ignores
`ApiKey.expires_at`, and it returns the scopes stored at mint time. Remote MCP
gates its write tools on scope alone, so these keep write access:
- viewer keys minted before #1911;
- keys of members who were later demoted;
- keys of users who were removed from the org.

## Proposed outcome

- An expired key fails authentication on every acw_ path.
- A key's effective scopes are capped per call by the owner's current
  `OrgMember` role in `key.org_id`: below member, only `mcp:read`; no
  membership, no scopes; member and above keep the stored scopes. Keys with
  no owner (`user_id` None) are unchanged.

## Affected users and systems

AIFactory web-server `mcp_remote/auth.py`. This covers holders of expired,
demoted or orphaned keys. One extra indexed query runs per acw_ request.

## Constraints

No schema change. No revocation of rows, because the cap is computed rather
than stored. No gitops changes. Use the existing `ROLE_LEVELS` and `mcp:read`.

## Open questions

None. The cross-org remote MCP gap stays with #1910.
