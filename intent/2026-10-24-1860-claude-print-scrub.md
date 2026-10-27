---
status: approved
issue: AIFactory#1860
author: olafkfreund
---

# Intent: scrub outbound PII on the two direct `claude --print` spawns

## Problem

Two web-server paths start the `claude --print` CLI themselves and put the
prompt straight into argv, without passing it through `core.outbound_scrub`:

- `apps/web-server/server/services/clarification_service.py`
  (`generate_clarification_questions`, `claude --print --model haiku`): the
  task title and description go out raw.
- `apps/web-server/server/services/insights_service.py`
  (`InsightsService.generate_task_from_chat`): the whole chat transcript goes
  out raw.

Every other AIFactory LLM path scrubs by default and always scrubs for a
`regulated` project (#1862, Factory#320). These two do not scrub at any data
class, so a regulated project's PII (SSN, email, phone, card numbers) can
reach the provider. The Factory data-classification policy lists this as a
"known exception" until it is fixed.

## Proposed outcome

- Both spawns put the scrubbed prompt in argv whenever
  `scrub_outbound_enabled(classification)` is true, the same rule as the
  insights chat strategies.
- A `regulated` project always gets the scrubbed prompt, even with the
  kill-switch `LITELLM_AUDIT_SCRUB_OUTBOUND=false`.
- An `internal` project behaves like every other outbound path: scrubbed by
  default, raw only when the operator kill-switch is set.
- Tests read the argv the CLI was actually handed and prove both cases.
- The Factory policy no longer names a known exception, and the matching
  "two unscrubbed CLI spawns" notes in the risk register and statement of
  applicability go too (a small Factory PR into `main`).

## Affected users and systems

- AIFactory web-server: task clarification questions, "create task from
  chat" in Insights.
- Factory repo: `docs/compliance/policies/data-classification-and-handling-policy.md`,
  `docs/compliance/risk-register.md` (R-004), `docs/compliance/statement-of-applicability.md` (A.8.11).
- Operators of regulated projects; auditors reading the policy.

## Constraints

- Reuse what exists: `scrub_insights_outbound` / `scrub_outbound_prompt`,
  and `project_settings_store.data_classification(project_path)` as #1862 does
  (fails closed to `regulated` when unreadable).
- Fail closed: if the scrub cannot run, nothing is sent.
- Clarification must still never block the user: on failure it returns its
  existing `skip=True` default.
- No new flag, no wider PII pattern set (#1010).
- Only the wire is scrubbed; stored session messages keep what the user typed.
- The Factory doc PR merges only after the AIFactory fix is merged, so the
  policy never claims a fix that has not landed.

## Open questions

1. Where does the classification come from?
   Decided (lead recommendation, user-approved 2026-10-24): from
   `project_settings_store.data_classification(project_path)`, the way #1862
   does it, passed explicitly to the scrub.
2. What does "internal keeps today's behaviour" mean, given the brief also
   says to honour `scrub_outbound_enabled(classification)`?
   Decided (lead recommendation, user-approved 2026-10-24): `internal`
   follows `scrub_outbound_enabled`, as every other path does: scrub on by
   default, raw prompt only under the kill-switch. The internal test pins the
   kill-switch case (raw prompt reaches argv, as today) and the default case.
3. Which Factory docs change?
   Decided (lead recommendation, user-approved 2026-10-24): the "Known
   exception" sentence in the policy, plus the clauses in R-004 and A.8.11
   that describe the same gap, so the three documents agree.
