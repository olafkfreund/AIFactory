---
status: approved
issue: 1969
spec: spec/2026-10-30-1969-model-from-source.md
coders:
  1: agy  # the lead's brief names agy (Antigravity) for this lane
  2: agy
---

# Plan: coder and QA model/thinking selection reads the source spec dir

Approved decisions (from the spec): model and thinking selection for coder,
parallel and QA sessions comes from `source_spec_dir or spec_dir`; no new
helper; `qa/commands` uses `_source_project_dir` (#1955) and falls back to
`Path(os.devnull)` (no metadata, defaults) when it returns None;
`qa/correction.py` and `agents/token_attribution.py` unchanged. (The spec's
"`run_parallel_phase_integration`" is `run_parallel_coding_phase`.)

All paths below are relative to the repo root. Python:
`/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python`.

## Steps

1. Coder and parallel runner.
   - `apps/backend/agents/coder.py` ~457-458: pass `gate_dir` (defined at
     ~214) instead of `spec_dir` to `get_phase_model` and
     `get_phase_thinking_budget`; update the comment above to cite #1969
     ("from the source copy, which the agent cannot edit").
   - `apps/backend/agents/parallel_integration.py` ~458-459 in
     `run_parallel_coding_phase`: pass `source_spec_dir or spec_dir`.
   - `tests/test_plan_review_gate_snapshot.py`: add an optional
     `extra: dict | None = None` param to `_drive`, merged over `patchers`.
     Add `test_coder_model_comes_from_source_not_worktree_metadata`: source
     `_make_spec(tmp_path, {"requireReviewBeforeCoding": False})`, worktree =
     `shutil.copytree(source, tmp_path / "worktree")` then
     `_write_meta(worktree, {"requireReviewBeforeCoding": False,
     "pinnedModel": "haiku", "thinkingLevel": "ultrathink"})`; `extra` wraps the
     real `phase_config.get_phase_model` / `get_phase_thinking_budget` in
     `MagicMock(side_effect=...)` that records return values; drive with
     `next_subtask={"id": "1", "description": "d"}` and
     `source_spec_dir=source`; assert `resolve_model_id("haiku")` is not among
     recorded models and `get_thinking_budget("ultrathink")` is not among
     recorded budgets. `monkeypatch.delenv("AIFACTORY_FALLBACK_MODEL",
     raising=False)` and `AIFACTORY_ROUTING_POLICY` likewise.
   → check: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_plan_review_gate_snapshot.py -q`
   (the venv path above); the new test must fail with the two source edits reverted.

2. QA loop and its callers.
   - `apps/backend/qa/loop.py` `run_qa_validation_loop`: add keyword param
     `source_spec_dir: Path | None = None` (docstring: canonical spec dir when
     `spec_dir` is a worktree copy; model/thinking come from it, #1969); set
     `model_dir = source_spec_dir or spec_dir` near the top; pass `model_dir`
     to every `get_phase_model` / `get_phase_thinking_budget` call in the
     function (~232, ~233, ~335, ~336, ~541, ~542, ~635). Nothing else moves.
   - `apps/backend/cli/build_commands.py` ~618: pass
     `source_spec_dir=source_spec_dir`.
   - `apps/backend/cli/qa_commands.py` `handle_qa_command`: hoist
     `root = _source_project_dir(project_dir)` and the source spec dir
     (`get_specs_dir(root) / spec_dir.name if root else None`, importing
     `get_specs_dir` locally as today) above the plan-seed `try`, reuse it for
     `gate_dir` there, and pass
     `source_spec_dir=gate_dir or Path(os.devnull)` to
     `run_qa_validation_loop` with a one-line comment (no source: no metadata,
     defaults). Add `import os` if missing.
   - New `tests/test_qa_model_from_source.py`: worktree spec dir with
     `QA_FIX_REQUEST.md` and metadata `{"phaseModels": {"qa": "haiku",
     "qa_fixer": "haiku"}, "phaseThinking": {"qa": "none", "qa_fixer": "none"}}`;
     source spec dir with `{}`. Patch, in `run_qa_validation_loop.__globals__`
     (see `tests/test_git_metadata_baseline.py` ~635 for why):
     `is_build_complete`→True, `create_client`→a recording MagicMock returning
     an async-context client, `run_qa_fixer_session`→AsyncMock(("fixed","")),
     `_create_qa_reviewer_provider`→MagicMock raising a sentinel exception to
     stop the loop, plus whatever else the loop touches before that (task
     logger, emit_phase, status/git-metadata checks). Call with
     `source_spec_dir=source`, `model="sonnet"`; assert the fixer
     `create_client` model and the reviewer model are not
     `resolve_model_id("haiku")` and the thinking budgets are not
     `get_thinking_budget("none")`. Clear `AIFACTORY_FALLBACK_MODEL` and
     `AIFACTORY_ROUTING_POLICY`.
   → check: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_qa_model_from_source.py ../../tests/test_profile_untrusted.py ../../tests/test_git_metadata_baseline.py ../../tests/test_qa_loop.py -q`;
   the new test must fail with the `qa/loop.py` edits reverted.

## Tests

Each step's check; ruff 0.14.10 check + format on touched files; the cq ratchet
as CI runs it (`scripts/cq_ratchet.py --base origin/dev --ruff <ruff 0.14.10>
--config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py'
'scripts/*.py'`); CI runs the full suite.

## Rollback

Revert the commits.
