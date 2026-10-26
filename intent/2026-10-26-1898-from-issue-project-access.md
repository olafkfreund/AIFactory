---
status: draft
issue: 1898
author: olafkfreund
---

# Intent: project access check on from-issue intake

## Problem

`POST /api/tasks/from-issue` starts a governed build in the project named in its body, but nothing checks that the caller may act on that project. The router (`apps/web-server/server/routes/from_issue.py:55`), the route (`create_from_issue`) and its include (`server/main.py:590`) carry no dependency; only `TokenAuthMiddleware` authenticates. Any authenticated principal, for example an `acw_` key of another org, can start a build in any project. `create-and-run` already requires `member` on its project (`require_project_access("member")`).

## Proposed outcome

- A caller who is not at least `member` of the target project's org gets 403 (404 for an unknown project, as elsewhere).
- Members, the legacy service token (`is_service`) and auth-disabled deployments keep working.
- The intake poller keeps creating tasks. It calls the route over HTTP with `AIFACTORY_TOKEN`/`APP_API_TOKEN`, the legacy service principal (`services/intake_poller.py` `_api_token`, `_route_low_medium`).

## Affected users and systems

AIFactory web-server from-issue route; the intake poller (unchanged); any external caller of the route.

## Constraints

- Same rule as the other project routes (`authorize_project_for_user`, minimum role `member`); no new authorization logic.
- The request contract stays as it is: `project_id` stays in the body, and no query parameter is added. Changing it would break the poller and the GH Action callers.
- No lockout of the service token or a member.

## Open questions

None.
