"""Tests for the silent no-op build guard (#779).

A run that finishes with subtasks planned but ZERO completed must be treated
as a failure (run.py exits 1), so a headless build Job is marked Failed
instead of Complete with an empty branch. Legitimate 0-completed exits
(PAUSE file, plan-review human_review pause) must NOT trip the guard.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from cli.build_commands import build_is_silent_noop


def _write_plan(
    spec_dir: Path,
    statuses: list[str],
    plan_status: str | None = None,
) -> None:
    plan: dict[str, Any] = {
        "phases": [
            {"subtasks": [{"id": f"s{i}", "status": s} for i, s in enumerate(statuses)]}
        ]
    }
    if plan_status is not None:
        plan["status"] = plan_status
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )


def test_zero_completed_is_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["pending", "pending", "failed"])
    assert build_is_silent_noop(tmp_path) is True


def test_all_failed_is_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["failed", "failed"])
    assert build_is_silent_noop(tmp_path) is True


def test_any_completed_is_not_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["completed", "pending", "failed"])
    assert build_is_silent_noop(tmp_path) is False


def test_no_plan_is_not_noop(tmp_path: Path) -> None:
    assert build_is_silent_noop(tmp_path) is False


def test_empty_plan_is_not_noop(tmp_path: Path) -> None:
    _write_plan(tmp_path, [])
    assert build_is_silent_noop(tmp_path) is False


def test_pause_file_excluded(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["pending", "pending"])
    (tmp_path / "PAUSE").write_text("", encoding="utf-8")
    assert build_is_silent_noop(tmp_path) is False


def test_plan_review_pause_excluded(tmp_path: Path) -> None:
    _write_plan(tmp_path, ["pending", "pending"], plan_status="human_review")
    assert build_is_silent_noop(tmp_path) is False


def test_unreadable_plan_status_still_noop(tmp_path: Path) -> None:
    # count_subtasks reads the same file; a corrupt plan yields total == 0 so
    # the guard never fires — never fail a build on unreadable metadata.
    (tmp_path / "implementation_plan.json").write_text("{not json", encoding="utf-8")
    assert build_is_silent_noop(tmp_path) is False


# ── #1422: completed subtasks are a claim, not evidence ───────────────────────
#
# A run reported "All subtasks completed!", pushed its branch and advanced its
# card while the branch tip was byte-identical to its base. The subtask counter
# and the worktree disagreed, and only the counter was consulted. These cover the
# case the counter cannot see.


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo)},
    )


def _repo_with_base(tmp_path: Path) -> Path:
    """A repo whose base commit is reachable from a fake ``origin`` remote."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "base.txt").write_text("base\n", encoding="utf-8")
    _git(origin, "add", "base.txt")
    _git(origin, "commit", "-qm", "base")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    return work


def test_completed_but_empty_worktree_is_noop(tmp_path: Path) -> None:
    """The #1422 case: subtasks claim completion, git shows nothing."""
    spec = tmp_path / "spec"
    spec.mkdir()
    _write_plan(spec, ["completed", "completed"])
    work = _repo_with_base(tmp_path)
    assert build_is_silent_noop(spec, work) is True


def test_completed_with_a_real_commit_is_not_noop(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    spec.mkdir()
    _write_plan(spec, ["completed", "completed"])
    work = _repo_with_base(tmp_path)
    (work / "feature.txt").write_text("real work\n", encoding="utf-8")
    _git(work, "add", "feature.txt")
    _git(work, "commit", "-qm", "implement the thing")
    assert build_is_silent_noop(spec, work) is False


def test_completed_with_uncommitted_changes_is_not_noop(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    spec.mkdir()
    _write_plan(spec, ["completed"])
    work = _repo_with_base(tmp_path)
    (work / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    assert build_is_silent_noop(spec, work) is False


def test_without_work_dir_the_counter_still_decides(tmp_path: Path) -> None:
    """Callers that pass no worktree keep the pre-#1422 behaviour."""
    _write_plan(tmp_path, ["completed", "pending"])
    assert build_is_silent_noop(tmp_path) is False
