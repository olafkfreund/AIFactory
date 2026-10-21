"""#1617 — a subtask coder must find its spec inside its own worktree.

A parallel subtask runs in a fresh git worktree, which is also the sandbox root
and the base any repo-relative path resolves against. That worktree carries no
`.aifactory/specs`, and the code fell back to the shared spec dir — a path
*outside* the worktree.

Measured on task 022: every Read of `spec.md` and `implementation_plan.json`
failed ("File does not exist", against a doubled path), 26 failures against 32
successes in one window, and the build coded all 21 subtasks without ever
reading its plan. Nothing in the phase output said so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.parallel_integration import _seed_child_spec  # noqa: E402


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared" / "specs" / "022-demo"
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# the spec\n")
    (d / "implementation_plan.json").write_text('{"phases": []}\n')
    # Control-plane state that must NOT be forked into a subtask worktree.
    (d / "task_logs.json").write_text("[]\n")
    return d


def test_spec_and_plan_land_inside_the_worktree(spec_dir: Path, tmp_path: Path):
    """THE regression: the coder's own worktree must contain its documents."""
    child = tmp_path / "wt" / ".aifactory" / "specs" / "022-demo"

    used = _seed_child_spec(spec_dir, child)

    assert used == child, "must use the in-worktree copy, not the shared dir"
    assert (child / "spec.md").read_text() == "# the spec\n"
    assert (child / "implementation_plan.json").exists()


def test_control_plane_state_is_not_copied(spec_dir: Path, tmp_path: Path):
    """Only the two documents the prompt names — the rest is not a subtask's."""
    child = tmp_path / "wt" / ".aifactory" / "specs" / "022-demo"

    _seed_child_spec(spec_dir, child)

    assert not (child / "task_logs.json").exists()


def test_missing_documents_fall_back_and_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """An empty spec dir is the old behaviour plus a warning, not a crash.

    The silence is what made this expensive: a build that never read its plan
    looked exactly like one that did.
    """
    empty = tmp_path / "shared" / "specs" / "022-demo"
    empty.mkdir(parents=True)
    child = tmp_path / "wt" / ".aifactory" / "specs" / "022-demo"

    with caplog.at_level("WARNING"):
        used = _seed_child_spec(empty, child)

    assert used == empty
    assert any("without its spec" in r.message for r in caplog.records)


def test_unwritable_target_falls_back_and_warns(
    spec_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    """A copy failure degrades to the previous behaviour rather than failing the build."""
    child = tmp_path / "wt" / ".aifactory" / "specs" / "022-demo"

    def _boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("agents.parallel_integration.shutil.copy2", _boom)

    with caplog.at_level("WARNING"):
        used = _seed_child_spec(spec_dir, child)

    assert used == spec_dir
    assert any("falling back" in r.message for r in caplog.records)
