---
status: approved
issue: 1921
intent: intent/2026-10-27-1921-sync-allowlist.md
---

# Spec: allowlist the worktree extra-files sync

## Design

In `agent_worktree_sync.py`, replace the "copy every top-level file not in
`known_files`" loop with a loop over a fixed tuple of build-produced files:

- `token_usage.json` (read by `routes/tasks_usage.py`, `completion.py`)
- `injection_scan.json`, `guardrail_halt.json` (read by `completion.py`, written
  by the build into its spec dir)
- `handback_received.json` (read by `pr_endgame.merge_gate_signals`; its presence
  only tightens the gate)

The Factory#1960 `never_sync` set becomes redundant (an allowlist cannot reach
those names) and is removed; its comment moves to the allowlist. No code lists
the spec dir generically (checked: no `iterdir`/`glob` over a spec dir in the
web server), so nothing else depends on the generic mirror.

## Alternatives rejected

- Extend the denylist with `task_metadata.json`, `review_state.json`, markers:
  the next control file added to the spec dir is open again.
- Mirror `*.md` as the old comment suggested ("plan .md files"): no reader
  exists, and `TFACTORY_HANDOFF.md`/`skill_context.md` are control-owned.

## Risks

A build-produced file a reader needs and this list misses stops reaching the
main spec dir on subprocess hosts. Mitigated by the reader grep above; kubejob
is unaffected.

## Verification

Regression test in `tests/test_task_control_plane.py`: agent-written
`task_metadata.json` (lower `reviewTier`, other tenant, no budget, other base),
`review_state.json` (approved), `COMPLETED.json` and an unknown file in the
worktree leave the canonical copies unchanged, and `gather_pr_context`-level
reads (`review_tier_of`, `spec_tenant`) still return the canonical values;
each of the four allowlisted files is still mirrored. Fails on
the old loop. Existing sync tests stay green.
