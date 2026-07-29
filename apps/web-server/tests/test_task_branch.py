"""Resolving which branch holds a task's work (#1073).

Against real git repositories, not mocks: the bug was entirely about what git
reports in a specific worktree state, which a mock would have been written to
match the wrong assumption.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


def _resolver():
    return pytest.importorskip("server.services.task_branch").resolve_task_branch


def _git(*args: str, cwd: Path) -> None:
    # S603/S607: fixed literals in a test fixture, no shell, no external input.
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


def _clone_on_base(repo: Path, dest: Path) -> None:
    """A checkout of the project sitting on the base branch.

    This is what the kubejob backend leaves behind: the task directory is its
    own checkout on `main`, never switched to the task branch. `git worktree
    add <path> main` cannot reproduce it -- git refuses to check out a branch
    that is already checked out elsewhere.
    """
    subprocess.run(  # noqa: S603
        ["git", "clone", "-q", str(repo), str(dest)],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on `main` with one commit."""
    p = tmp_path / "project"
    p.mkdir()
    _git("init", "-q", "-b", "main", cwd=p)
    _git("config", "user.email", "t@example.com", cwd=p)
    _git("config", "user.name", "t", cwd=p)
    (p / "f.txt").write_text("x")
    _git("add", ".", cwd=p)
    _git("commit", "-qm", "init", cwd=p)
    return p


def test_the_kubejob_case_a_worktree_left_on_main(repo: Path, tmp_path: Path) -> None:
    """THE bug. The worktree is on main; the work is on a branch beside it.

    Reading the worktree's HEAD gave "main", so create-pr asked GitHub to open
    main -> main and the Approve button could not work at all.
    """
    _git("branch", "aifactory/097-thing", cwd=repo)
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt, project_path=repo, spec_id="097-thing", base_branch="main"
    )
    assert err is None, err
    assert branch == "aifactory/097-thing"


def test_the_subprocess_case_worktree_is_on_the_task_branch(
    repo: Path, tmp_path: Path
) -> None:
    """The in-pod backend builds IN the worktree; its HEAD is authoritative."""
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "aifactory/098-other", str(wt), cwd=repo)

    branch, err = _resolver()(
        worktree_path=wt, project_path=repo, spec_id="098-other", base_branch="main"
    )
    assert err is None, err
    assert branch == "aifactory/098-other"


def test_it_never_returns_the_base_branch(repo: Path, tmp_path: Path) -> None:
    """The original defect, pinned directly.

    No branch exists for this spec, so the answer must be a refusal -- never
    "main", which is what silently merging into itself looked like.
    """
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt, project_path=repo, spec_id="099-missing", base_branch="main"
    )
    assert branch is None
    assert err and "099-missing" in err


def test_a_suffix_is_not_a_substring(repo: Path, tmp_path: Path) -> None:
    """`001-add-thing-extra` must not answer a lookup for `001-add-thing`."""
    _git("branch", "aifactory/001-add-thing-extra", cwd=repo)
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt, project_path=repo, spec_id="001-add-thing", base_branch="main"
    )
    assert branch is None, f"matched the wrong branch: {branch}"
    assert err


def test_ambiguity_refuses_rather_than_picking_one(repo: Path, tmp_path: Path) -> None:
    """Merging the wrong task's branch is far worse than refusing."""
    _git("branch", "aifactory/100-thing", cwd=repo)
    _git("branch", "someoneelse/100-thing", cwd=repo)
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt, project_path=repo, spec_id="100-thing", base_branch="main"
    )
    assert branch is None
    assert err and "ambiguous" in err


def test_a_missing_worktree_still_resolves_from_the_branch(
    repo: Path, tmp_path: Path
) -> None:
    """A kubejob build may leave no worktree at all; the branch is enough."""
    _git("branch", "aifactory/101-thing", cwd=repo)
    branch, err = _resolver()(
        worktree_path=tmp_path / "nope",
        project_path=repo,
        spec_id="101-thing",
        base_branch="main",
    )
    assert err is None, err
    assert branch == "aifactory/101-thing"


def test_a_recorded_branch_is_preferred(repo: Path, tmp_path: Path) -> None:
    """Data beats archaeology: the build records what it pushed."""
    _git("branch", "custom/prefix/102-thing", cwd=repo)
    spec = repo / ".aifactory" / "specs" / "102-thing"
    spec.mkdir(parents=True)
    (spec / ".task_branch").write_text("custom/prefix/102-thing\n")
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt,
        project_path=repo,
        spec_id="102-thing",
        base_branch="main",
    )
    assert err is None, err
    assert branch == "custom/prefix/102-thing"


def test_a_stale_record_is_not_trusted(repo: Path, tmp_path: Path) -> None:
    """The record is VALIDATED, not obeyed.

    A branch recorded then deleted -- merged previously, or a reused spec dir --
    must fall through to discovery. Handing git a branch because a file says so
    is how you merge the wrong thing, or nothing.
    """
    _git("branch", "aifactory/103-thing", cwd=repo)
    spec = repo / ".aifactory" / "specs" / "103-thing"
    spec.mkdir(parents=True)
    (spec / ".task_branch").write_text("aifactory/deleted-long-ago\n")
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt,
        project_path=repo,
        spec_id="103-thing",
        base_branch="main",
    )
    assert err is None, err
    assert branch == "aifactory/103-thing", "a dead recorded branch must not win"


def test_a_record_naming_the_base_branch_is_ignored(repo: Path, tmp_path: Path) -> None:
    """Belt and braces on the one value that must never be returned."""
    _git("branch", "aifactory/104-thing", cwd=repo)
    spec = repo / ".aifactory" / "specs" / "104-thing"
    spec.mkdir(parents=True)
    (spec / ".task_branch").write_text("main\n")
    wt = tmp_path / "wt"
    _clone_on_base(repo, wt)

    branch, err = _resolver()(
        worktree_path=wt,
        project_path=repo,
        spec_id="104-thing",
        base_branch="main",
    )
    assert err is None, err
    assert branch == "aifactory/104-thing"
