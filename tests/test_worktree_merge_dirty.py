"""Regression for #485: `--merge` must not abort with a bogus 'Merge conflict'
when the base working tree is dirty on a tracked file (e.g. .gitignore).

The smart-merge step rewrites .gitignore in the base working tree before the
git merge, leaving it uncommitted. `git merge` then aborts with "Your local
changes to .gitignore would be overwritten by merge". merge_worktree now stashes
uncommitted base-tree changes before the merge (the worktree branch is the
source of truth) and drops the stash on success.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from worktree import WorktreeManager  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def test_merge_succeeds_with_dirty_gitignore_in_base(temp_git_repo: Path):
    # Base main: commit a tracked .gitignore.
    (temp_git_repo / ".gitignore").write_text("*.log\n")
    _git(temp_git_repo, "add", ".gitignore")
    _git(temp_git_repo, "commit", "-m", "add gitignore")

    mgr = WorktreeManager(temp_git_repo, base_branch="main")
    info = mgr.create_worktree("001-feature")
    wt = Path(info.path)

    # Worktree branch: add new files + change .gitignore, then commit.
    (wt / "main.go").write_text("package main\nfunc main() {}\n")
    (wt / ".gitignore").write_text("*.log\n/bin\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feature: main.go + gitignore")

    # Simulate the smart-merge dirtying the base working tree's .gitignore.
    (temp_git_repo / ".gitignore").write_text("*.log\n# touched by smart-merge\n")
    assert _git(temp_git_repo, "status", "--porcelain").stdout.strip(), (
        "base should be dirty"
    )

    # Before the fix this returned False ("Merge conflict! Aborting").
    ok = mgr.merge_worktree("001-feature", delete_after=False, no_commit=False)
    assert ok is True, "merge should succeed despite the dirty .gitignore"

    # The worktree branch's content landed on base.
    assert (temp_git_repo / "main.go").exists(), "main.go should be merged into base"
    # Tree is clean after the merge (stash was dropped, not left dangling).
    stashes = _git(temp_git_repo, "stash", "list").stdout
    assert "aifactory pre-merge" not in stashes, "pre-merge stash should be dropped"
