"""#1454: the pre-merge security gate on the real merge path.

With AIFACTORY_SELF_HEAL on, a gate that crashes must refuse the merge -- a
scan that did not run is not a clean scan. With it off, the same crash is
logged and the merge proceeds exactly as before.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents import self_heal_integration as shi  # noqa: E402
from worktree import WorktreeManager  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _feature_branch(repo: Path) -> WorktreeManager:
    mgr = WorktreeManager(repo, base_branch="main")
    wt = Path(mgr.create_worktree("001-feature").path)
    (wt / "feature.py").write_text("def add(a, b):\n    return a + b\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feature")
    return mgr


def _crash(*_a, **_k):
    raise RuntimeError("gate crashed")


def test_crashed_gate_refuses_merge_when_enabled(temp_git_repo, monkeypatch):
    monkeypatch.setenv("AIFACTORY_SELF_HEAL", "1")
    monkeypatch.setattr(shi, "security_pre_merge_gate_sync", _crash)
    mgr = _feature_branch(temp_git_repo)

    assert mgr.merge_worktree("001-feature", delete_after=False) is False
    assert not (temp_git_repo / "feature.py").exists()


def test_crashed_gate_does_not_block_when_disabled(temp_git_repo, monkeypatch):
    monkeypatch.delenv("AIFACTORY_SELF_HEAL", raising=False)
    monkeypatch.setattr(shi, "security_pre_merge_gate_sync", _crash)
    mgr = _feature_branch(temp_git_repo)

    assert mgr.merge_worktree("001-feature", delete_after=False) is True
    assert (temp_git_repo / "feature.py").exists()
