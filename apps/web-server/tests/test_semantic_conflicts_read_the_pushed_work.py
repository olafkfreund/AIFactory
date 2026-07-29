"""The SEMANTIC half of merge preview must read the pushed work too (#1089).

`#1083` fixed the git half of `/worktree/merge-preview`: the file list, commit
count and `specBranch` all come from ``resolve_work_ref`` and are correct. The
semantic half did not move. It runs

    conflict_service.detect_conflicts -> MergeOrchestrator
      -> evolution_tracker.refresh_from_git -> git diff {base}...HEAD  (cwd=worktree)

and under the kubejob backend the control plane's worktree sits on the BASE
branch, so that diff is ``base...base``. The semantic conflict detector was
handed an EMPTY change set and reported zero conflicts for every task, beside a
correct file list -- a half-fixed surface reads as a working one.

Neither existing seam test could catch it.
``test_merge_preview_shows_the_pushed_work`` asserts the git half, and
``test_resolve_uncommitted_sees_the_task_files`` patches
``conflict_service.get_conflict_service`` with a stub, so it mocks past exactly
the path that was broken.

These tests use real git repositories. ``git worktree add`` cannot reproduce the
shape because it SHARES refs with the project repo, which hides the bug.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
_BACKEND = _WEB_SERVER.parent / "backend"
for _p in (str(_WEB_SERVER), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SPEC_ID = "098-semantic-half"
TASK_BRANCH = f"aifactory/{SPEC_ID}"
TASK_ID = f"proj-1:{SPEC_ID}"


def _git(args: list[str], cwd: Path) -> None:
    # S603/S607: fixed literals in a test fixture, no shell, no external input.
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def kubejob_shape(tmp_path: Path) -> dict[str, Path]:
    """origin + project clone on main + a task branch pushed by the build Job."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "--bare"], origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    _git(["config", "user.email", "t@t"], seed)
    _git(["config", "user.name", "t"], seed)
    (seed / "app.py").write_text("def handler():\n    return 'v1'\n")
    _git(["add", "."], seed)
    _git(["commit", "-qm", "seed"], seed)
    _git(["remote", "add", "origin", str(origin)], seed)
    _git(["push", "-q", "origin", "main"], seed)

    # The control plane's project checkout.
    project = tmp_path / "project"
    _git(["clone", "-q", str(origin), str(project)], tmp_path)
    _git(["config", "user.email", "t@t"], project)
    _git(["config", "user.name", "t"], project)

    # The task worktree: a standalone clone, left on main. This is the shape
    # that made the bug invisible -- it looks like a workspace and contains
    # none of the work.
    worktree = project / ".aifactory" / "worktrees" / "tasks" / SPEC_ID
    worktree.parent.mkdir(parents=True)
    _git(["clone", "-q", "--no-hardlinks", str(project), str(worktree)], tmp_path)

    # The build Job: a separate clone that does the work and pushes it.
    job = tmp_path / "job"
    _git(["clone", "-q", str(origin), str(job)], tmp_path)
    _git(["config", "user.email", "t@t"], job)
    _git(["config", "user.name", "t"], job)
    _git(["checkout", "-q", "-b", TASK_BRANCH], job)
    # A real SEMANTIC change (a new function), not just a literal tweak:
    # get_task_modifications filters on snapshot.semantic_changes, so a
    # cosmetic edit would be dropped by the production query and the test
    # would be measuring the analyser rather than the diff.
    (job / "app.py").write_text(
        "def handler():\n    return 'v2'\n\n\ndef added_by_the_task():\n"
        "    return 'new'\n"
    )
    (job / "feature.py").write_text("def added():\n    return True\n")
    _git(["add", "."], job)
    _git(["commit", "-qm", "the task's work"], job)
    _git(["push", "-q", "origin", TASK_BRANCH], job)

    # The project repo learns about the branch, as the control plane would.
    _git(["fetch", "-q", "origin"], project)

    return {"project": project, "worktree": worktree, "origin": origin}


def _tracker(project: Path):
    """A FileEvolutionTracker with baselines captured, as production has.

    ``record_modification`` only records files that already carry a baseline, so
    a tracker with none records nothing no matter what the diff says. Capturing
    baselines first is what makes this a test of the DIFF rather than of the
    baseline precondition.
    """
    tracker_mod = pytest.importorskip("merge.file_evolution.tracker")
    tracker = tracker_mod.FileEvolutionTracker(project)
    tracker.capture_baselines(TASK_ID, [project / "app.py"])
    return tracker


def _modified(tracker) -> dict[str, object]:
    return {
        Path(rel).name: snap for rel, snap in tracker.get_task_modifications(TASK_ID)
    }


def test_the_worktree_read_sees_nothing_which_is_the_defect(
    kubejob_shape: dict[str, Path],
) -> None:
    """Documents the bug: reading the worktree records no modification.

    Kept as a test rather than a comment so the premise stays true. If the
    worktree ever DOES hold the work, this fails and the fix can be
    reconsidered rather than quietly carrying an obsolete workaround.
    """
    tracker = _tracker(kubejob_shape["project"])
    tracker.refresh_from_git(TASK_ID, kubejob_shape["worktree"], target_branch="main")
    assert _modified(tracker) == {}, (
        "the control-plane worktree is on the base branch and holds none of the "
        "work, so base...HEAD there must be empty"
    )


def test_the_ref_read_sees_the_pushed_work(kubejob_shape: dict[str, Path]) -> None:
    """The fix: given the ref and the repo, the task's change is recorded."""
    tracker = _tracker(kubejob_shape["project"])
    tracker.refresh_from_git(
        TASK_ID,
        kubejob_shape["worktree"],
        target_branch="main",
        work_ref=f"origin/{TASK_BRANCH}",
        repo_path=kubejob_shape["project"],
    )

    mods = _modified(tracker)
    assert "app.py" in mods, (
        f"the semantic detector was handed {sorted(mods) or 'nothing'} -- it must "
        "see what the build pushed, or it reports zero conflicts for every task"
    )


def test_the_new_content_comes_from_the_ref_not_the_filesystem(
    kubejob_shape: dict[str, Path],
) -> None:
    """The trap inside the fix.

    Fixing only the diff command leaves the per-file "content after" read
    hitting ``worktree_path / file_path``. Those files do not exist in the
    control plane's worktree, so each would be recorded with empty content --
    i.e. as a DELETION. That is worse than seeing nothing: it invents conflicts
    rather than missing them.
    """
    tracker = _tracker(kubejob_shape["project"])
    tracker.refresh_from_git(
        TASK_ID,
        kubejob_shape["worktree"],
        target_branch="main",
        work_ref=f"origin/{TASK_BRANCH}",
        repo_path=kubejob_shape["project"],
    )

    snap = _modified(tracker).get("app.py")
    assert snap is not None, "app.py was not recorded against the task"
    seen = " ".join(
        str(getattr(snap, attr, "") or "")
        for attr in ("content_after", "raw_diff", "content_hash_after")
    )
    assert "v2" in seen or (snap.raw_diff and "+" in snap.raw_diff), (
        "the task's new content is missing -- it was read from the worktree "
        f"filesystem, where these files do not exist. got {seen!r}"
    )


def test_the_route_passes_the_resolved_ref_to_the_detector() -> None:
    """The wiring, asserted at the seam.

    The helper accepting a ref is useless if the route keeps calling it without
    one, and that is a single-line regression away.
    """
    src = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    call = src.index("await conflict_service.detect_conflicts(")
    args = src[call : src.index(")", call)]
    assert "work_ref=work_ref" in args, (
        "merge-preview must hand the detector the ref resolve_work_ref found"
    )
    assert "repo_path=" in args, "the detector needs a repo to resolve the ref in"
