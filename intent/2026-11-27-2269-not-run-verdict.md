---
status: approved
issue: 2269
author: olafkfreund
---

# Intent: a `not_run` TFactory verdict no longer holds the merge gate forever

## Problem

AIFactory's merge gate reads TFactory's `verdicts.json` through `tfactory_verdict`. Any verdict
other than `accept`, `flag` or `reject` makes it return "no trusted result", and the gate holds.

TFactory records `not_run` for a test that gave no evidence. Its own VAL levels exclude those
rows. In E2E run `mvr-20261127w` (Factory#3882), one `not_run` row held a PR until the run timed
out.

## Proposed outcome

`not_run` rows are ignored. The gate decides on the remaining rows:
- a `reject` among them fails it;
- otherwise accept or flag passes it.

If no row remains, there is still no trusted result, so the gate holds. Any other unknown value
still holds too.

## Affected users and systems

AIFactory `apps/backend/pfactory/tfactory_client.py` (`tfactory_verdict`) and its tests. Every
auto-merge gate reads TFactory through this function.

## Constraints

- **Fail-closed:** a run where every row is `not_run`, or where an unknown value appears, never
  passes.
- **AIFactory release only.**

## Open questions

None.
