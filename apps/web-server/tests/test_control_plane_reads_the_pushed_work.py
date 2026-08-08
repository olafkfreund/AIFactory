"""The control-plane CLI surfaces must read the pushed work, not the worktree HEAD.

#1089 allowlisted ``core/worktree.py`` and ``merge/timeline_git.py`` as
"agent-side, so the worktree HEAD IS the task branch". Re-tracing the entry
points shows that verdict was too broad -- both are also reachable from
``run.py`` on the CONTROL plane, where the worktree is a standalone clone left
on the base branch:

    run.py --spec X --review    -> cli.workspace_commands.handle_review_command
      -> core.workspace.finalization.review_existing_build
        -> WorktreeManager(project_dir) -> show_build_summary/show_changed_files
          -> WorktreeManager.get_changed_files    (worktree.py)

    run.py --spec X --merge     -> cli.workspace_commands.handle_merge_command
      -> core.workspace.merge_existing_build -> _try_smart_merge_inner
        -> FileTimelineTracker.capture_worktree_state
          -> TimelineGitHelper.get_changed_files_in_worktree  (timeline_git.py)

Both read ``{base}...HEAD`` in the worktree, which under kubejob is
``base...base``: ``--review`` printed "No changes were made." for every task and
the smart merge started from an empty timeline.

The fixture below is the kubejob shape, built for real rather than mocked: an
``origin`` carrying the pushed task branch, and a build directory that is a
clone sitting on the base branch, exactly as
``build_backend._populate_self_contained_worktree`` leaves it. Each test asserts
BOTH halves -- that the old worktree-HEAD read sees nothing (so these tests fail
against unfixed code rather than passing vacuously) and that the fixed read sees
the work.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "backend"


def _load(name: str, relative: str):
    """Import a backend module straight off disk.

    Both modules under test are deliberately stdlib-only (``core/worktree.py``
    carries a shim at ``apps/backend/worktree.py`` whose whole purpose is to keep
    it importable without core's dependencies), so this needs no backend venv.
    """
    spec = importlib.util.spec_from_file_location(name, _BACKEND / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - literal "git", no shell, test-local args
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def kubejob_project(tmp_path: Path) -> dict:
    """A project repo whose task work exists ONLY on a pushed branch.

    Returns the project dir, the build directory (on the base branch, as the
    control plane leaves it), and the spec id.
    """
    spec_id = "042-feature"
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)

    # The project checkout the control plane runs in.
    project = tmp_path / "project"
    _git("clone", str(origin), str(project), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=project)
    _git("config", "user.name", "t", cwd=project)
    (project / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=project)
    _git("commit", "-m", "base", cwd=project)
    _git("push", "-u", "origin", "main", cwd=project)

    # The build Job's work: committed on aifactory/<spec> and pushed. Nothing
    # about it ever touches the control plane's filesystem.
    _git("checkout", "-b", f"aifactory/{spec_id}", cwd=project)
    (project / "feature.py").write_text("def added_by_the_task():\n    return 1\n")
    (project / "README.md").write_text("base\ntouched by the task\n")
    _git("add", "feature.py", "README.md", cwd=project)
    _git("commit", "-m", "the task's work", cwd=project)
    _git("push", "origin", f"aifactory/{spec_id}", cwd=project)
    _git("checkout", "main", cwd=project)
    _git("branch", "-D", f"aifactory/{spec_id}", cwd=project)

    # The build directory: a standalone clone left on the BASE branch (#716).
    build_dir = project / ".aifactory" / "worktrees" / "tasks" / spec_id
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--branch", "main", str(origin), str(build_dir), cwd=tmp_path)

    return {"project": project, "build_dir": build_dir, "spec_id": spec_id}


def test_review_and_merge_see_the_pushed_files(kubejob_project: dict) -> None:
    """WorktreeManager.get_changed_files: the --review/--merge/--discard read."""
    worktree = _load("wt_under_test", "core/worktree.py")
    manager = worktree.WorktreeManager(kubejob_project["project"], base_branch="main")

    # The read this replaces, run against the same fixture: the build directory's
    # HEAD is the base branch, so the range is base...base.
    assert (
        manager._run_git(
            ["diff", "--name-status", "main...HEAD"], cwd=kubejob_project["build_dir"]
        ).stdout.strip()
        == ""
    )

    assert manager.discover_pushed_ref(kubejob_project["spec_id"]) == (
        "origin/aifactory/042-feature"
    )
    changed = {
        path: status
        for status, path in manager.get_changed_files(kubejob_project["spec_id"])
    }
    assert changed == {"feature.py": "A", "README.md": "M"}

    # get_change_summary is what show_build_summary prints; "No changes were
    # made." was the user-visible symptom.
    summary = manager.get_change_summary(kubejob_project["spec_id"])
    assert summary["new_files"] == 1
    assert summary["modified_files"] == 1


def test_nothing_pushed_still_reads_the_worktree(kubejob_project: dict) -> None:
    """The in-Job / subprocess shape must be byte-for-byte unchanged.

    With no pushed ref the worktree HEAD genuinely IS the task branch, and that
    is the majority caller. A fix that only worked for kubejob would break it.
    """
    worktree = _load("wt_under_test", "core/worktree.py")
    manager = worktree.WorktreeManager(kubejob_project["project"], base_branch="main")

    _git(
        "push",
        "origin",
        "--delete",
        f"aifactory/{kubejob_project['spec_id']}",
        cwd=kubejob_project["project"],
    )
    _git("fetch", "--prune", "origin", cwd=kubejob_project["project"])
    assert manager.discover_pushed_ref(kubejob_project["spec_id"]) is None

    # Commit the work in the build directory instead, the subprocess-backend shape.
    build_dir = kubejob_project["build_dir"]
    _git("config", "user.email", "t@example.com", cwd=build_dir)
    _git("config", "user.name", "t", cwd=build_dir)
    _git("checkout", "-b", "aifactory/042-feature", cwd=build_dir)
    (build_dir / "local_only.py").write_text("x = 1\n")
    _git("add", "local_only.py", cwd=build_dir)
    _git("commit", "-m", "in-worktree work", cwd=build_dir)

    changed = manager.get_changed_files(kubejob_project["spec_id"])
    assert changed == [("A", "local_only.py")]


def test_the_timeline_capture_reads_the_ref_not_the_filesystem(
    kubejob_project: dict,
) -> None:
    """TimelineGitHelper: the smart-merge baseline behind run.py --merge.

    The content half matters as much as the file list. The build directory is
    checked out at the BASE branch, so reading ``worktree_path / file_path``
    after fixing only the diff would record base content as the task's version --
    asserting the task changed nothing rather than merely failing to see it.
    """
    timeline_git = _load("timeline_git_under_test", "merge/timeline_git.py")
    helper = timeline_git.TimelineGitHelper(kubejob_project["project"])
    build_dir = kubejob_project["build_dir"]
    ref = "origin/aifactory/042-feature"

    # Unfixed: the worktree read finds nothing.
    assert helper.get_changed_files_in_worktree(build_dir, target_branch="main") == []

    changed = helper.get_changed_files_in_worktree(
        build_dir, target_branch="main", work_ref=ref
    )
    assert sorted(changed) == ["README.md", "feature.py"]

    # The filesystem still holds base content for README.md and no feature.py at
    # all -- which is why the content must come from the ref too.
    assert "touched by the task" not in (build_dir / "README.md").read_text()
    assert not (build_dir / "feature.py").exists()
    assert helper.get_file_content_at_commit("feature.py", ref) == (
        "def added_by_the_task():\n    return 1\n"
    )
