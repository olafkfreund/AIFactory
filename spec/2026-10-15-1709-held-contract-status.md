---
status: approved
issue: 1709
intent: intent/2026-10-15-1709-held-contract-status.md
---

# Spec: show a held task contract on the task status

## Design

**D1. One plan key, `contractHold`.** A hold writes a short string onto the
spec's `implementation_plan.json` under the new key `contractHold`, e.g.
`"migration contract held (digest mismatch); build stopped"`. It is the same
text the `[trusted-contract]` log line already prints, without the prefix, so
it holds only `reason_label` constants (never the env value or a digest).
The plan file is the carrier because it is the one spec file a packed Job
pushes back on every exit, `sys.exit` included (`cli/main.py:526-532`).

**D2. Neutral by construction.** `contractHold` is a new key. It never touches
`status`, `reviewReason`, `planStatus` or subtask states. Every consumer that
branches on task status reads only those (`task_service.load_spec_metadata`,
`process_monitor` / `is_failed_build`, `agent_service._update_plan_status`,
CFactory sync via `task_to_dict`, the frontend `reviewReasonBadge`), so none of
them sees the hold. `_update_plan_status` read-modify-writes the plan and keeps
unknown keys.

**D3. Writer.** `core.contract_trust.record_hold(spec_dir, note: str | None)`:
reads `implementation_plan.json`; if it is not a JSON object, does nothing (it
never creates a plan, which could look like a finished planning phase); sets
the key to `note`, or removes it when `note` is None; skips the write when
nothing changes; swallows `OSError`. Never raises.

**D4. Call sites.**
- `cli/build_commands._resolve_build_contract`: on a non-hold state,
  `record_hold(spec_dir, None)` (a later good build clears a stale note); on a
  hold, `record_hold(spec_dir, <msg without prefix>)` before the existing
  `sys.exit(1)` / `return None`.
- `agents/deploy_scaffold.scaffold_deploy_for_spec`: on hold,
  `record_hold(spec_dir, "deploy scaffold withheld: <label>")`.

**D5. Surface.** `task_service.task_to_dict` already reads the plan for the
archive fields (`task_service.py:1284-1296`); it also sets
`result["contractHold"]` when the plan holds a string. The frontend `Task` type
gets `contractHold?: string`; `TaskHeader` shows an `outline` (neutral) badge
"Contract held" with the note as its tooltip, at any status.

**D6. Migration hold still exits 1.** Unchanged (approved #1673 decision); the
note explains the failure. Recorded as open question 1 in the intent.

## Alternatives rejected

- **A new `reviewReason` value.** `reviewReason` drives the board column,
  the `REVIEW_REASON_BADGES` record, CFactory's mapping and failure counts; a
  hold reason there would be read as a review state.
- **A `status` change or `failed` phase.** That is exactly what must not happen.
- **A separate `contract_hold.json`.** Not pushed back from packed Jobs.
- **A DB column / pydantic `Task` field.** More surface than a display note
  needs; `task_to_dict` is what the UI and the API list return.
- **Exiting 0 on a held migration.** Changes an approved security decision;
  out of scope for a visibility task.

## Risks

- A plan read-modify-write could race a concurrent plan writer. The two writes
  happen at build start and at scaffold time (build end), when no other writer
  runs; the write is best-effort.
- A hold before any plan exists is not recorded (logs still show it). Contracts
  arrive through the trusted-plan path, which writes the plan at intake
  (`trusted_plan.py:846`), so this is rare.
- The WebSocket status event does not carry the note; it appears on the next
  task list fetch.

## Verification

- Unit: `record_hold` sets, clears, ignores a missing/non-object plan, never
  raises.
- `_resolve_build_contract`: held migration records the note then exits 1;
  held normal build records it and returns None; verified clears it, and
  `status`/`reviewReason` are unchanged in every case.
- Deploy scaffold withheld records the note.
- `task_to_dict` returns `contractHold` from the plan.
- `npx tsc --noEmit` for the frontend change.
