---
status: draft
issue: 1969
intent: intent/2026-10-30-1969-model-from-source.md
---

# Spec: coder and QA model/thinking selection reads the source spec dir

## Design

Reuse the existing idiom; no new helper.

- `agents/coder.py` `run_autonomous_agent`: `get_phase_model` and
  `get_phase_thinking_budget` take `gate_dir` (already
  `source_spec_dir or spec_dir`, #1952/#1967) instead of `spec_dir`.
- `agents/parallel_integration.py` `run_parallel_phase_integration` (the
  function at ~430): the same two calls take `source_spec_dir or spec_dir`.
- `qa/loop.py` `run_qa_validation_loop` gains `source_spec_dir: Path | None = None`;
  `model_dir = source_spec_dir or spec_dir`; every `get_phase_model` /
  `get_phase_thinking_budget` call in it (fixer-first ~232, reviewer ~335,
  fixer ~541, failover ~635) takes `model_dir`. Everything else
  (reports, plan, `create_client`'s spec_dir) stays on `spec_dir`.
- Callers: `cli/build_commands.py` passes its `source_spec_dir`;
  `cli/qa_commands.py` passes the source spec dir it already derives with
  `_source_project_dir` (#1955): `get_specs_dir(root) / spec_dir.name`. When
  `_source_project_dir` returns None (a path under `.aifactory/worktrees`
  that is not a task root) it passes `Path(os.devnull)`, so no metadata is
  read and the phase defaults/CLI model apply (fail closed).
- `qa/correction.py`: unchanged. Its callers (REST `apply-correction`,
  completion orchestration) pass the original `.aifactory/specs/<id>` dir,
  not a worktree copy.

## Alternatives rejected

- Snapshot the selection at run start: also correct, but a second mechanism
  beside the established `gate_dir` idiom, and it still has to read a dir.
- Make the worktree `task_metadata.json` read-only: the agent can still
  replace the file; wider change (#1932 territory).
- Fix `agents/token_attribution.py` here: telemetry only, needs threading the
  source dir through accounting; follow-up issue.

## Risks

A legitimate per-run model change written only into the worktree copy is
ignored; nothing writes it there by design. Direct mode: `source_spec_dir` is
None, so behaviour is identical.

## Verification

- `tests/test_plan_review_gate_snapshot.py`: source metadata with no model
  choice, worktree copy with `pinnedModel: haiku` and `thinkingLevel:
  ultrathink`; with the real `get_phase_model`/`get_phase_thinking_budget`,
  the coder session never resolves the haiku model or the ultrathink budget.
- New `tests/test_qa_model_from_source.py`: worktree copy names
  `phaseModels.qa`/`qa_fixer` = haiku and `phaseThinking` none; QA fixer-first
  and reviewer resolve from the source (defaults), not haiku.
- Both fail on `dev` and pass with the fix. CI ruff 0.14.10, cq ratchet,
  targeted pytest.
