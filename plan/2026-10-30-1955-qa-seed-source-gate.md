---
status: draft
issue: 1955
spec: spec/2026-10-30-1955-qa-seed-source-gate.md
coders:
  1: codex  # the lead's brief names Codex for this lane
  2: codex  # same
---

# Plan: QA plan-command seeding reads the approval gate from the source spec dir

Approved decisions (from the spec): `gate_dir = get_specs_dir(project_dir) /
spec_dir.name`; seed `plan=None` only if `gate_dir.is_dir()` and
(`not requires_review_before_coding(gate_dir)` or
`ReviewState.load(gate_dir).is_approval_valid(spec_dir)`), else `plan={}`.
`find_spec` and `cli/main.py` are unchanged.

## Steps

1. `apps/backend/cli/qa_commands.py` ~111-133 (the seeding `try` block): add
   the `gate_dir` lookup (import `get_specs_dir` from `spec.pipeline` inside the
   same local-import block, with the existing `# noqa: PLC0415` style) and the
   condition above; one comment citing #1955 (worktree copy is agent-writable;
   no source → no grants). Keep the broad `except`.
   `tests/test_profile_untrusted.py`: in `test_qa_entry_seeds_plan_by_review_state`
   pass `proj / ".aifactory" / "specs" / "spec"` (create it) as the spec dir.
   Add `test_qa_entry_reads_gate_from_source_not_worktree`, parametrised over
   `forge_review` / `flip_metadata`, using real `ReviewState` and
   `requires_review_before_coding` (only `print_banner`,
   `validate_environment`, `should_run_qa`, `run_qa_validation_loop` and
   `seed_profile_with_plan_commands` patched as in the existing test):
   source `proj/.aifactory/specs/001-x` with `spec.md`, metadata
   `{"requireReviewBeforeCoding": true}`, plan with `required_commands: ["uv"]`,
   `ReviewState().approve(source)`; worktree copy
   `proj/.aifactory/worktrees/tasks/001-x/.aifactory/specs/001-x`
   (`shutil.copytree`), plan widened to `["uv", "npx"]`, then either
   `ReviewState().approve(worktree)` or metadata `{"requireReviewBeforeCoding": false}`
   → `seen == [{}]`. Plus `test_qa_entry_worktree_without_source_seeds_nothing`
   (source dir removed, worktree approved and unchanged → `[{}]`).
   → verify: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_profile_untrusted.py -q`; ruff check + format on both files; CI ratchet.
   Traps: other tests swap the `review` package, so patch module objects via
   `importlib.import_module`, never dotted strings; `get_specs_dir` appends to
   `.gitignore` on first use (harmless in tmp_path).

2. (Revision after the lead's review.) `apps/backend/cli/qa_commands.py`: add
   a module-level helper `_source_project_dir(project_dir: Path) -> Path | None`
   that finds `(".aifactory", "worktrees")` in `project_dir.resolve().parts`;
   if absent, returns `project_dir`; if present and the path is exactly
   `.aifactory/worktrees/tasks/<name>`, returns the root above `.aifactory`;
   otherwise `None`. In `handle_qa_command`, `gate_dir` is
   `get_specs_dir(root) / spec_dir.name` when the root is not `None`, and the
   seed is `None` only when `gate_dir` exists and passes the step 1 check;
   else `{}`. Keep the helper out of `handle_qa_command`'s branch count
   (strict-ruff ratchet PLR0912).
   Tests in `tests/test_profile_untrusted.py`: the reviewer's probe
   (`project_dir` = worktree root, worktree metadata flipped and plan widened
   → `[{}]`); a worktree-root `project_dir` with an approved, unchanged source
   → `[None]`; a `project_dir` deeper under `.aifactory/worktrees/` → `[{}]`.
   → verify: as step 1.

## Tests

The step's check, then the full backend suite as CI runs it.

## Rollback

Revert the commit.
