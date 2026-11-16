---
status: approved
issue: 2033
author: olafkfreund
---

# Intent: bound agent runs with a token ceiling and a default-on guardrail

## Problem

An agent run has no effective stop. `max_turns` is 1000, the anti-loop
guardrail (#474) is off unless `AIFACTORY_ACT_GUARDRAIL` is set, and nothing
halts a run after it used a set number of tokens. The RFC-0014 budget check is
a pre-run estimate in `observe` mode with no caller in the run path.

## Proposed outcome

- The guardrail is active with no environment variables set; `off` opts out.
- A per-run token ceiling with a default halts the build loop and the
  completion event carries a typed `halt_reason` naming the ceiling.
- `max_turns` is configurable. `environment-reference.md` shows the defaults.
- PFactory and TFactory get a linked follow-up (they have no guardrail at all).

## Affected users and systems

AIFactory backend build and QA loops, completion event, operators' env docs.

## Constraints

Must not break a run when a hook errors. Ceiling and guardrail must be
switchable off. No live-cluster change.

## Open questions

None. Default ceiling value is a judgement call (see spec).
