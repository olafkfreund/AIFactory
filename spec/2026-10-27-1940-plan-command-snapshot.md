---
status: draft
issue: 1940
intent: intent/2026-10-27-1940-plan-command-snapshot.md
---

# Spec: plan-command grants come only from the plan the build was allowed to start with

## Design

Snapshot once per run, before any agent session, and seed from the snapshot.

- `project/analyzer.py` `seed_profile_with_plan_commands(project_dir, plan_path, plan=None)`:
  new keyword `plan`. When given, it is used instead of reading `plan_path`.
  `None` keeps today's behaviour (read the file).
- `agents/coder.py`: new helper `_approved_plan_snapshot(spec_dir, require_review)`.
  Returns `None` when review is not required (callers keep reading the live
  plan, as today). When review is required it returns the plan parsed once at
  run start, or `{}` if none exists or it cannot be read (grants nothing).
  `run_autonomous_agent` calls it right after `require_review` is computed
  (that value is already read before any agent session, #1922), and passes the
  result as `plan=` at the per-session seed call and through
  `_maybe_run_parallel_phase` to `run_parallel_coding_phase`.
- `agents/parallel_integration.py` `run_parallel_coding_phase(..., approved_plan=None)`:
  passes it as `plan=` to the wave-child seed call.

Why this is the gated copy: a review-required run reaches a coder session only
when the plan already existed at start and passed the run.py pre-flight
`is_approval_valid` (or an explicit `--force`). When the planner runs in the
same run, the review gate returns right after it, so no coder session follows;
the snapshot is `{}` then and grants nothing to the planner, which needs none.

Profiles keep earlier grants (`custom_commands` only grows), so the snapshot
can only stop widening, never revoke. That matches "narrowing is fine".

## Alternatives rejected

- Re-check `is_approval_valid` before each seeding (issue's alternative): it
  reads the same agent-writable spec dir, and a hash miss would need a policy
  (skip? pause the build?). The snapshot needs neither.
- Snapshot bound to the approval hash in a control-plane store: larger change
  across web-server/kubejob for the same effect within a run.
- Snapshot after the planner for no-review runs: changes no-review behaviour,
  which the intent rules out; with no approval the agent's plan is the authority.

## Risks

- A legitimately approved plan whose later subtasks need a tool not declared at
  approval time is now blocked until re-approval. That is the intended trade.
- Profile direct-write (intent, open question) still lets an agent widen its
  allowlist; follow-up issue.

## Verification

`tests/test_plan_seed.py` new cases:
- review required: snapshot, then agent adds `npx` to the live plan; seeding
  with the snapshot does not grant `npx`; approved `uv`/`pytest` granted.
- review required, no plan at start: snapshot is `{}`, nothing granted.
- no review required: helper returns `None`, seeding reads the live plan and
  grants the added command (as today).
Plus ruff, the mypy ratchet and the existing seed/review tests.
