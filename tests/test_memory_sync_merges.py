"""No memory sync may delete another task's insights (#1033).

Two mechanisms write a spec's ``memory/`` back from a worktree:

* ``agent_worktree_sync._sync_worktree_files`` — a visibility mirror, ticking
  while an in-pod build runs so the context API can show insights early;
* ``agents.utils.sync_memory_to_source`` — the durable write, running inside the
  build so it also covers Job-dispatched builds (#1030).

Having two is fine. Having one that REPLACES is not: the destination is a store
that accumulates across tasks, and the directory sync used to ``rmtree`` it
before copying. That is safe only while the worktree copy is a superset of the
destination — true at seed time, false as soon as anything else writes. Under
RFC-0016 concurrency, two tasks can build the same spec and the slower one's
replace silently discards the faster one's session insights.

These tests pin the property both mechanisms must share, rather than either
implementation.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _seam in (_ROOT / "apps" / "backend", _ROOT / "apps" / "web-server"):
    if str(_seam) not in sys.path:
        sys.path.insert(0, str(_seam))

from agents.utils import sync_memory_to_source  # noqa: E402


def _insight(spec: Path, name: str, body: str) -> None:
    d = spec / "memory" / "session_insights"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def _names(spec: Path) -> set[str]:
    d = spec / "memory" / "session_insights"
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


# ── the concurrency case #1033 asks for ──────────────────────────────────────


def test_two_tasks_on_one_spec_keep_both_sets_of_insights(tmp_path):
    """MUTATION GUARD: the data-loss bug, asserted at the shape that causes it.

    Task A and task B build the same spec from their own worktrees. Both were
    seeded before either wrote, so NEITHER worktree contains the other's file —
    which is exactly when a replace destroys data.
    """
    source = tmp_path / "specs" / "001"
    (source / "memory" / "session_insights").mkdir(parents=True)

    wt_a = tmp_path / "wt-a" / "001"
    wt_b = tmp_path / "wt-b" / "001"
    for wt in (wt_a, wt_b):
        (wt / "memory" / "session_insights").mkdir(parents=True)

    _insight(wt_a, "session_001.json", '"A learned the retry loop deadlocks"')
    _insight(wt_b, "session_001.json".replace("001", "002"), '"B learned the cache is cold"')

    # A finishes first, then B. B's worktree has never seen A's file.
    assert sync_memory_to_source(wt_a, source)
    assert sync_memory_to_source(wt_b, source)

    assert _names(source) == {"session_001.json", "session_002.json"}
    text = (source / "memory" / "session_insights" / "session_001.json").read_text()
    assert "deadlocks" in text, "the earlier task's insight was destroyed"


def test_the_same_sync_run_twice_is_harmless(tmp_path):
    """The mirror ticks every few seconds; re-running must not churn or lose."""
    source = tmp_path / "specs" / "001"
    (source / "memory").mkdir(parents=True)
    wt = tmp_path / "wt" / "001"
    _insight(wt, "session_001.json", '"x"')

    assert sync_memory_to_source(wt, source)
    assert sync_memory_to_source(wt, source)
    assert _names(source) == {"session_001.json"}


def test_a_rewritten_file_still_wins(tmp_path):
    """Merging must not mean stale: the build that owns a file overwrites it."""
    source = tmp_path / "specs" / "001"
    _insight(source, "session_001.json", '"old"')
    wt = tmp_path / "wt" / "001"
    _insight(wt, "session_001.json", '"new"')

    assert sync_memory_to_source(wt, source)
    assert (source / "memory" / "session_insights" / "session_001.json").read_text() == '"new"'


# ── the mirror must not reintroduce replace semantics ────────────────────────


def test_the_directory_mirror_does_not_wipe_its_destination():
    """MUTATION GUARD: `rmtree` must not return to the dirs_to_sync loop.

    Asserted on the source rather than by driving AgentService, which needs a
    running build to exercise. The property is narrow and the regression would be
    a one-line edit, so pinning the line is proportionate — and a comment
    mentioning rmtree must not be enough to satisfy it.
    """
    src = (
        _ROOT / "apps" / "web-server" / "server" / "services" / "agent_worktree_sync.py"
    ).read_text()

    loop = src.split("for dirname in dirs_to_sync:", 1)[1]
    assert "shutil.rmtree" not in loop, (
        "the directory sync wipes its destination again — that deletes another "
        "task's memory (#1033)"
    )
    assert "dirs_exist_ok=True" in loop


@pytest.mark.parametrize("mechanism", ["mirror", "durable"])
def test_both_mechanisms_merge(mechanism, tmp_path):
    """Whichever writes memory, the accumulated store survives.

    Parametrised so adding a third mechanism has an obvious place to be proved.
    """
    source = tmp_path / "specs" / "001"
    _insight(source, "existing.json", '"kept"')
    wt = tmp_path / "wt" / "001"
    _insight(wt, "incoming.json", '"added"')

    if mechanism == "durable":
        sync_memory_to_source(wt, source)
    else:
        # The mirror's operation, isolated: merge the directory, do not replace it.
        shutil.copytree(wt / "memory", source / "memory", dirs_exist_ok=True)

    assert _names(source) == {"existing.json", "incoming.json"}


# ── path-injection barrier (CodeQL py/path-injection) ────────────────────────


def test_a_spec_id_cannot_escape_the_project():
    """MUTATION GUARD: spec_id reaches this module from the API and is
    interpolated into two filesystem paths. Unchecked, `../../etc` walks out of
    the project and the sync copies into somewhere it was never meant to touch.
    """
    from server.services.agent_worktree_sync import _safe_spec_component

    for bad in ("../../etc", "..", ".", "a/b", "/abs", "", "a\\b", "a\x00b"):
        with pytest.raises(ValueError):
            _safe_spec_component(bad)


def test_a_normal_spec_id_is_unchanged():
    from server.services.agent_worktree_sync import _safe_spec_component

    assert _safe_spec_component("001-fix-bug") == "001-fix-bug"


def test_the_barrier_is_actually_applied_before_any_path_is_built():
    """MUTATION GUARD: a barrier that exists but is not called is decoration.

    Deleting the call site is a one-line refactor that leaves every direct test
    of `_safe_spec_component` green while the path-injection returns — I found
    exactly that by mutating this file, which is why the assertion is on the
    CALL and its position rather than on the helper.
    """
    src = (
        _ROOT / "apps" / "web-server" / "server" / "services" / "agent_worktree_sync.py"
    ).read_text()
    body = src.split("async def _sync_worktree_files", 1)[1]

    call = body.find("_safe_spec_component(spec_id)")
    first_path = body.find("worktree_spec = (")
    assert call != -1, "spec_id is no longer sanitised (CodeQL py/path-injection)"
    assert 0 <= call < first_path, "the barrier must run BEFORE spec_id reaches a path"
