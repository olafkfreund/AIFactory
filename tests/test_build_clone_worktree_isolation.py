"""#1467 — the build clone must not clobber the finished task worktree.

``build_backend`` replaced ``.aifactory/worktrees/tasks/<spec>`` (a REAL linked
worktree, registered in the parent repo and holding the task branch) with a
standalone clone on the base branch, and never deregistered it. The orphaned
registration keeps the branch lock, so:

* ``routes/pr.py`` — which pushes from exactly that path — no longer sees the
  branch (``src refspec ... does not match any``); and
* TFactory's ``git_writer`` cannot check the branch out
  (``already used by worktree at ...``).

Real git throughout: the defect is entirely about on-disk git state, so a mocked
version of it would assert nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from server.services import build_backend as bb  # noqa: E402

_SPEC = "042-go"
_BRANCH = f"aifactory/{_SPEC}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _project_with_finished_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose task worktree is a linked worktree holding built commits."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )

    proj = tmp_path / "workspaces" / "proj-1"
    proj.mkdir(parents=True)
    _git(proj, "init", "-b", "main")
    _git(proj, "config", "user.email", "t@example.com")
    _git(proj, "config", "user.name", "Test")
    _git(proj, "remote", "add", "origin", str(remote))
    (proj / "README.md").write_text("base\n")
    _git(proj, "add", "README.md")
    _git(proj, "commit", "-m", "base")
    _git(proj, "push", "-u", "origin", "main")

    spec_dir = proj / ".aifactory" / "specs" / _SPEC
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# spec\n")

    # The finished build: a real linked worktree on the task branch, carrying a
    # commit that exists nowhere else (the ``e45aff1`` of the live run).
    task_wt = proj / ".aifactory" / "worktrees" / "tasks" / _SPEC
    _git(proj, "worktree", "add", "-b", _BRANCH, str(task_wt), "main")
    (task_wt / "built.py").write_text("def slugify(s: str) -> str:\n    return s\n")
    _git(task_wt, "add", "built.py")
    _git(task_wt, "commit", "-m", "build output")
    assert (task_wt / ".git").is_file()  # linked worktree: .git is a FILE
    return proj, task_wt


def test_build_clone_leaves_the_task_worktree_deliverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a dispatch, the built branch is still pushable from where PR runs."""
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    proj, task_wt = _project_with_finished_worktree(tmp_path)
    built = _git(task_wt, "rev-parse", "HEAD")

    populated = bb.populate_build_worktree(proj, _SPEC)
    assert populated is not None

    # routes/pr.py pushes from this directory. It must find the branch — this is
    # the measured live failure ("src refspec ... does not match any").
    push = subprocess.run(
        ["git", "-C", str(task_wt), "push", "--dry-run", "origin", _BRANCH],
        capture_output=True,
        text=True,
    )
    assert push.returncode == 0, push.stderr

    # The task worktree is untouched: still linked, still on the task branch,
    # still holding the build.
    assert (task_wt / ".git").is_file()
    assert _git(task_wt, "rev-parse", "HEAD") == built
    assert _git(task_wt, "rev-parse", "--abbrev-ref", "HEAD") == _BRANCH

    # No orphaned registration is left holding the branch lock (the thing that
    # stops TFactory's git_writer checking the branch out).
    assert bb.orphaned_worktree_registrations(proj) == []
    assert Path(populated) != task_wt  # the clone got its own path


def test_orphaned_registration_is_detected_and_a_clean_tree_is_not(
    tmp_path: Path,
) -> None:
    """The #1467 invariant, both directions."""
    proj, task_wt = _project_with_finished_worktree(tmp_path)

    # Clean: every registered worktree is a linked worktree on disk.
    assert bb.orphaned_worktree_registrations(proj) == []

    # Orphaned: the registration survives, the path is a standalone clone.
    shutil.rmtree(task_wt)
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(proj), str(task_wt)],
        check=True,
        capture_output=True,
    )
    assert (task_wt / ".git").is_dir()
    # git worktree prune does NOT clear it — the path still exists.
    _git(proj, "worktree", "prune")
    assert bb.orphaned_worktree_registrations(proj) == [str(task_wt)]
