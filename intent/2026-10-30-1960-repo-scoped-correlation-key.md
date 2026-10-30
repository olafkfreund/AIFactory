---
status: approved
issue: 1960
author: olafkfreund
---

# Intent: never emit a bare issue number as the correlation key

## Problem

`correlation_key()` in `apps/web-server/server/services/completion.py` returns
`str(issue_number)` when the spec has no source repo. Since CFactory #506,
CFactory matches keys exactly with no bare-number fallback, so issue 7 in two
repos would share one work item. The TFactory handoff mirror
(`apps/backend/pfactory/tfactory_client.py` `_event_correlation_key`) emits
the same bare key.

## Proposed outcome

AIFactory never emits a bare number as a correlation key. A spec with an issue
but no known repo correlates on the synthetic `af-<spec_id>`, and the TFactory
handoff carries the same key so both factories still join.

## Affected users and systems

AIFactory web server completion events, task rows, from-issue responses; the
TFactory handoff contract. CFactory (read-only consumer, accepts any string).

## Constraints

- Repo-scoped keys (`<repo>#<n>`) must not change.
- The handoff key must stay equal to the event key (pinned by a test).
- No CFactory change.

## Open questions

None.
