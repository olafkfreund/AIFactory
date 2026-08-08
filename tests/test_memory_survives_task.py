"""Agent memory survives a task (#1030).

The bug: session insights were written to
``<worktree>/.aifactory/specs/<spec>/memory/`` and only ``implementation_plan.json``
was synced back, so ``memory/`` died with the worktree. On the live cluster all 8
insight files sat inside a worktree; none had ever reached a source spec dir. The
fleet accumulated memory *within* a task (sessions share a worktree) and threw it
away at teardown, so the second pass over a codebase cost exactly what the first
did.

These tests pin the END-TO-END property, not the copy. It is easy to write a test
that proves a file was copied and still ship a system that forgets, because the
thing that matters is whether the NEXT task can read what the last one learned.
That round trip is what ``test_memory_written_in_one_task_is_readable_by_the_next``
asserts, going through the real seeding function rather than asserting on paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.utils import sync_memory_to_source  # noqa: E402
from core.workspace.setup import copy_spec_to_worktree  # noqa: E402


def _spec(root: Path, name: str) -> Path:
    d = root / name
    (d / "memory" / "session_insights").mkdir(parents=True)
    (d / "implementation_plan.json").write_text("{}")
    return d


# ── the round trip that is the whole point ───────────────────────────────────


def test_memory_written_in_one_task_is_readable_by_the_next(tmp_path):
    """MUTATION GUARD: the reported bug, asserted end to end.

    Task 1 writes an insight inside its worktree; task 2 gets a fresh worktree
    seeded from source. Task 2 must be able to read what task 1 learned.
    """
    source = _spec(tmp_path / "project" / ".aifactory" / "specs", "001-feature")

    # --- task 1: worktree seeded from source, learns something ---
    wt1 = tmp_path / "worktrees" / "task-1"
    wt1.mkdir(parents=True)
    spec_in_wt1 = copy_spec_to_worktree(source, wt1, "001-feature")
    (spec_in_wt1 / "memory" / "session_insights" / "session_001.json").write_text(
        '{"what_failed": ["the retry loop deadlocks under load"]}'
    )
    assert sync_memory_to_source(spec_in_wt1, source)

    # --- task 1's worktree is torn down ---
    import shutil

    shutil.rmtree(wt1)

    # --- task 2: a brand-new worktree, seeded the way the runner seeds it ---
    wt2 = tmp_path / "worktrees" / "task-2"
    wt2.mkdir(parents=True)
    spec_in_wt2 = copy_spec_to_worktree(source, wt2, "001-feature")

    carried = spec_in_wt2 / "memory" / "session_insights" / "session_001.json"
    assert carried.exists(), "task 2 cannot see what task 1 learned"
    assert "deadlocks under load" in carried.read_text()


def test_without_the_sync_the_next_task_starts_blind(tmp_path):
    """The control: the exact pre-fix behaviour, so the guard above means something.

    Identical to the test above except the sync never runs. If this ever starts
    finding the file, the round-trip test is passing for some other reason.
    """
    source = _spec(tmp_path / "project" / ".aifactory" / "specs", "001-feature")

    wt1 = tmp_path / "worktrees" / "task-1"
    wt1.mkdir(parents=True)
    spec_in_wt1 = copy_spec_to_worktree(source, wt1, "001-feature")
    (spec_in_wt1 / "memory" / "session_insights" / "session_001.json").write_text("{}")
    # (no sync — this is what shipped)

    import shutil

    shutil.rmtree(wt1)

    wt2 = tmp_path / "worktrees" / "task-2"
    wt2.mkdir(parents=True)
    spec_in_wt2 = copy_spec_to_worktree(source, wt2, "001-feature")

    assert not (
        spec_in_wt2 / "memory" / "session_insights" / "session_001.json"
    ).exists()


# ── the sync itself ──────────────────────────────────────────────────────────


def test_it_merges_rather_than_replaces(tmp_path):
    """MUTATION GUARD: losing memory is the failure being fixed.

    A sync that replaced the source tree would delete earlier tasks' insights —
    turning the fix into a subtler version of the same bug.
    """
    source = _spec(tmp_path / "specs", "001")
    (source / "memory" / "session_insights" / "session_001.json").write_text('"older"')

    wt_spec = _spec(tmp_path / "wt" / "specs", "001")
    (wt_spec / "memory" / "session_insights" / "session_002.json").write_text('"newer"')

    assert sync_memory_to_source(wt_spec, source)

    insights = source / "memory" / "session_insights"
    assert (insights / "session_001.json").read_text() == '"older"'
    assert (insights / "session_002.json").read_text() == '"newer"'


def test_it_is_a_no_op_outside_worktree_mode(tmp_path):
    """Same dir means the runner already wrote durably; copying onto itself is
    at best pointless and at worst a way to truncate a file with itself."""
    source = _spec(tmp_path / "specs", "001")
    assert sync_memory_to_source(source, source) is False


def test_no_source_is_a_no_op(tmp_path):
    source = _spec(tmp_path / "specs", "001")
    assert sync_memory_to_source(source, None) is False


def test_a_spec_with_no_memory_yet_is_a_no_op(tmp_path):
    wt = tmp_path / "wt" / "001"
    wt.mkdir(parents=True)
    source = _spec(tmp_path / "specs", "001")
    assert sync_memory_to_source(wt, source) is False


def test_a_failing_copy_never_raises(tmp_path, monkeypatch):
    """A build that produced working code must not fail because its memory
    could not be filed."""
    import agents.utils as utils

    source = _spec(tmp_path / "specs", "001")
    wt_spec = _spec(tmp_path / "wt" / "specs", "001")
    (wt_spec / "memory" / "session_insights" / "s.json").write_text("{}")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(utils.shutil, "copytree", boom)
    assert utils.sync_memory_to_source(wt_spec, source) is False


# ── it is wired into every branch, not just the happy one ────────────────────


@pytest.mark.parametrize("branch", ["completed", "incomplete", "failed"])
def test_every_session_outcome_files_its_memory(branch):
    """A session that failed or stalled is where the DEAD ENDS live.

    Syncing only on success would preserve the least valuable memory and discard
    the most valuable — RFC-0021 calls dead ends the category most worth keeping.
    So all three ``save_session_memory`` call sites must be followed by a sync.
    """
    src = (_BACKEND / "agents" / "session.py").read_text()
    assert src.count("sync_memory_to_source(spec_dir, source_spec_dir)") >= 3
    assert src.count("await save_session_memory(") == 3
