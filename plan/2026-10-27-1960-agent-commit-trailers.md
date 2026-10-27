---
status: approved
issue: 1960
spec: spec/2026-10-27-1960-agent-commit-trailers.md
---

# Plan: agent identity, originator and tier as git trailers on factory commits

Approved decisions (from the spec):

- New default-off flag `AIFACTORY_AGENT_COMMIT_TRAILERS`, truthy values `1`/`true`/`yes`/`on` (case-insensitive, stripped), same idiom as `apps/backend/core/auth.py:48`.
- One pure helper `commit_trailer_args(spec_dir: Path | None) -> list[str]` in a new `apps/backend/core/commit_trailers.py`. It reads `spec_dir / "requirements.json"` and returns `git commit` argv items, in order:
  `--trailer "Agent-Identity: <agent_identity>"`, `--trailer "Autonomy-Tier: <intake.autonomy_tier>"`, `--trailer "Originator: <originator>"`.
- A trailer is written only for a non-empty `str` value for which `str.isprintable()` holds (no `\n`/`\r`, no NUL or other control characters). Missing/`None`/non-string/empty/non-printable values are omitted. Deviation after review: the first version refused only `\n`/`\r`; a NUL byte got through and `subprocess` raised `ValueError`, which escapes the safety net's `except`. Flag off, `spec_dir=None`, missing file, invalid JSON, non-dict JSON, or `OSError` all return `[]`. The helper never raises.
- Values are written as recorded: the tier is the intake tier (`low`/`medium`/`hard`), not an A1-A3 disposition. No placeholders.
- Only the two AIFactory-run commit paths get trailers: `WorktreeManager.commit_in_worktree` and `commit_uncommitted_changes`. Each gains a keyword `spec_dir: Path | None = None`; `None` means today's argv exactly. Commit author is unchanged.
- Deviation after the lead's review (2026-10-27): the call sites use `commit_message_args(message, spec_dir)` (same module), which returns exactly `["-m", message]` when no trailers apply, and otherwise collapses the message to one printable line before the trailers. The message embeds agent-written subtask ids; a line break there forged trailers ahead of the real ones (reproduced with real git). Regression tests: `test_message_*` in `tests/test_commit_trailers.py` and `test_subtask_id_cannot_forge_trailers` in `tests/test_safety_net_commit.py`.
- Deviation after the lead's review: `agents/session.py` passes `source_spec_dir or spec_dir` to the safety net, so in worktree mode the trailers come from the canonical spec copy rather than the agent-writable worktree copy. The parallel path already passes the canonical `spec_dir`.
- Not covered either: the `merge --no-ff` commit in `core/worktree.py` (`aifactory: Merge <branch>`), which carries no trailers.
- Agent-session commits, squash-merge bodies, authorship and `merge_policy` mapping are out of scope.

## Steps

1. Create `apps/backend/core/commit_trailers.py` with `commit_trailer_args` as above (module docstring citing Factory#1960; `logging` not needed). Create `tests/test_commit_trailers.py` (same `sys.path.insert(0, ".../apps/backend")` header as `tests/test_safety_net_commit.py:16`) covering: flag off with a full file → `[]`; flag on, all three values → exact six-item list in order; each of missing / `None` / non-string / `""` / value with `\n` / value with `\r` → that trailer omitted, others kept; `spec_dir=None`, missing file, invalid JSON, JSON list → `[]`. Use `monkeypatch.setenv`/`delenv`.
   → verify: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_commit_trailers.py -q` (or the repo's usual pytest invocation) passes.
   Traps: none.

2. `apps/backend/core/worktree.py:1200-1213` `commit_in_worktree(self, spec_name, message, spec_dir: Path | None = None)`: change the commit argv to `[*pins, "commit", "-m", message, *commit_trailer_args(spec_dir)]`; import from `core.commit_trailers`. Update its one production caller `apps/backend/agents/parallel_integration.py:694-698` to pass `spec_dir=spec_dir` (the canonical spec dir already in scope there; `asyncio.to_thread` forwards kwargs). Add to `tests/test_worktree_commit_logging.py` a test that records the `_run_git` argv (pattern of `test_commit_success_returns_true`, line 57) and asserts: with the flag on and a `requirements.json` in a tmp spec dir, the `commit` call ends with the trailer args; with `spec_dir` omitted, the argv is exactly `[..., "commit", "-m", "msg"]`.
   → verify: `pytest tests/test_worktree_commit_logging.py tests/test_worktree_fresh_base.py -q` passes.
   Traps: `_run_git` prepends its own `-c core.bare=false`; assert on the args passed to it, not the final subprocess argv. Do not change `merge_driver_pins` or the `add` call.

3. `apps/backend/agents/utils.py:63` `commit_uncommitted_changes(project_dir, subtask_id=None, spec_dir: Path | None = None)`: change the commit at line ~123 to `["git", "commit", "-m", msg, *commit_trailer_args(spec_dir)]`; import from `core.commit_trailers`; mention `spec_dir` in the docstring in one line. Update the caller `apps/backend/agents/session.py:143` to `commit_uncommitted_changes(project_dir, subtask_id, spec_dir=spec_dir)`. Add to `tests/test_safety_net_commit.py` two real-git tests using the existing `repo` fixture: flag on + `requirements.json` with all three values in a tmp spec dir outside the repo → `git log -1 --format=%(trailers)` contains the three trailer lines; flag off with the same file → no trailers.
   → verify: `pytest tests/test_safety_net_commit.py -q` passes.
   Traps: keep the existing `try/except (CalledProcessError, OSError)` untouched; the safety net must never raise. Put the spec dir in `tmp_path` outside the repo, or it becomes staged content.

4. Checks for touched files: `ruff check` and `ruff format --check` on the five Python files, and the ratchet `scripts/cq_ratchet.py` (ruff and mypy, `--paths` limited to the touched files) shows no regressions. Deviation: the plan first named `scripts/ratchet_lint.py`, which does not exist in AIFactory.
   → verify: all clean.
   Traps: never `git add -A`; stage named paths only.

## Tests

- `pytest tests/test_commit_trailers.py tests/test_safety_net_commit.py tests/test_worktree_commit_logging.py tests/test_worktree_fresh_base.py -q` → all pass.
- Without step 1-3 code the new tests fail (import error / missing trailers), so they are regression tests.

## Rollback

Unset the flag (already the default) for an immediate no-op. Code rollback: revert the PR; no data, schema or config to undo.
