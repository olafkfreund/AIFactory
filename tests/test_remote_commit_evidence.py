"""The build's PUSHED branch is evidence the ledger cannot contradict (#1414).

#1070's gate condemned a real build -- one commit, five files, 29 passing tests
-- and with it skipped the TFactory handoff and the PR endgame. Git could not
answer (the kubejob path leaves the control-plane worktree on the base branch)
and the ledger answered `0` with total confidence.

The ledger was written once at build start and never appended to:
`record_good_commit` fires per SUBTASK and only when that session left a new
commit, so a build committing once at the end records nothing. `commits: []`
then reads as "committed nothing" rather than "nobody wrote anything down".

These tests build real git repositories with a real remote, because the thing
under test IS what git reports about a pushed branch. Mocking subprocess would
pass against a check that asked git the wrong question -- which is the entire
defect.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from pfactory.tfactory_client import (  # noqa: E402
    _remote_commit_count,
    build_commit_count,
)

SPEC_ID = "125-build-a-playable-tic-tac-toe-g"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A control-plane project whose origin holds a base branch."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-qm", "base")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True,
        capture_output=True,
    )

    proj = tmp_path / "project"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(proj)], check=True, capture_output=True
    )
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")

    spec = proj / ".aifactory" / "specs" / SPEC_ID
    spec.mkdir(parents=True)
    return proj


def _spec_dir(project: Path) -> Path:
    return project / ".aifactory" / "specs" / SPEC_ID


def _push_build_branch(project: Path, files: int) -> None:
    """Simulate the Job: commit on the build branch and push it to origin."""
    work = project.parent / "job"
    subprocess.run(
        ["git", "clone", "-q", str(project.parent / "origin.git"), str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-qb", f"aifactory/{SPEC_ID}")
    for i in range(files):
        (work / f"file{i}.txt").write_text(f"{i}\n")
        _git(work, "add", f"file{i}.txt")
        _git(work, "commit", "-qm", f"add file{i}")
    _git(work, "push", "-q", "origin", f"aifactory/{SPEC_ID}")


def test_a_pushed_commit_is_counted(project: Path) -> None:
    """The #1414 case: the build pushed, so origin knows."""
    _push_build_branch(project, files=1)

    assert _remote_commit_count(_spec_dir(project), SPEC_ID) == 1


def test_several_commits_are_counted(project: Path) -> None:
    _push_build_branch(project, files=3)

    assert _remote_commit_count(_spec_dir(project), SPEC_ID) == 3


def test_a_branch_with_no_commits_measures_zero(project: Path) -> None:
    """A real, measured zero must still be reported -- the gate depends on it."""
    _push_build_branch(project, files=0)

    assert _remote_commit_count(_spec_dir(project), SPEC_ID) == 0


def test_a_missing_branch_is_unknowable_not_zero(project: Path) -> None:
    """Fails to None. A `0` here would condemn every build origin cannot see."""
    assert _remote_commit_count(_spec_dir(project), SPEC_ID) is None


def test_a_non_repo_is_unknowable(tmp_path: Path) -> None:
    spec = tmp_path / "p" / ".aifactory" / "specs" / SPEC_ID
    spec.mkdir(parents=True)

    assert _remote_commit_count(spec, SPEC_ID) is None


def test_the_remote_overrules_an_empty_ledger(project: Path) -> None:
    """The whole point, end to end through the public entry point.

    An existing, empty ledger sits next to a pushed branch carrying a commit --
    exactly the state that condemned spec 125. `build_commit_count` must report
    the commit, not the ledger's confident zero.
    """
    _push_build_branch(project, files=1)
    spec = _spec_dir(project)
    memory = spec / "memory"
    memory.mkdir()
    (memory / "build_commits.json").write_text(
        json.dumps({"commits": [], "last_good_commit": None})
    )

    assert build_commit_count(spec, SPEC_ID) == 1


def test_the_ledger_still_answers_when_origin_cannot(project: Path) -> None:
    """The remote is preferred, not mandatory -- the ledger remains the fallback."""
    spec = _spec_dir(project)
    memory = spec / "memory"
    memory.mkdir()
    (memory / "build_commits.json").write_text(
        json.dumps({"commits": [{"hash": "abc123"}], "last_good_commit": "abc123"})
    )

    assert build_commit_count(spec, SPEC_ID) == 1
