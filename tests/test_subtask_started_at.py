"""``started_at`` must actually be written by both coding engines (#1195).

The field is not decorative. CFactory's live execution diagram reads it:

* ``taskFlow.flowStatus`` -- ``if (node.started_at && !node.completed_at)
  return "active"``, so a running subtask cannot be classified as active
  without it;
* ``taskFlow.nodeElapsedSeconds`` -- ``if (!node.started_at) return null``,
  and ``fmtClock(null)`` renders "", so every per-node timer chip was blank.

AIFactory served the field (``routes/task_service.py`` reads
``st.get("started_at")`` into the API ``Subtask`` model) but nothing ever wrote
it: ``Subtask.start()`` had no caller in either engine, and neither
``apply_subtask_status_update`` (serial) nor ``record_subtask_completion``
(wave) stamped it.

These tests pin the write at both funnels, and pin the timestamps into a single
timezone frame -- a naive local ``started_at`` against an aware UTC
``completed_at`` would hand the cockpit a duration out by the UTC offset.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.tools_pkg.tools.subtask import apply_subtask_status_update  # noqa: E402
from agents.utils import record_subtask_started  # noqa: E402
from implementation_plan.phase import Phase  # noqa: E402
from implementation_plan.plan import ImplementationPlan  # noqa: E402
from implementation_plan.subtask import Subtask  # noqa: E402


def _write_plan(spec_dir: Path, *ids: str) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    plan = ImplementationPlan(
        feature="greeting",
        phases=[
            Phase(
                phase=1,
                name="Implementation",
                subtasks=[Subtask(id=i, description=f"do {i}") for i in ids],
            )
        ],
    )
    path = spec_dir / "implementation_plan.json"
    plan.save(path)
    return path


def _subtask(path: Path, sid: str) -> dict:
    data = json.loads(path.read_text())
    for phase in data["phases"]:
        for st in phase["subtasks"]:
            if st["id"] == sid:
                return st
    raise AssertionError(f"subtask {sid} not in plan")


# ---------------------------------------------------------------------------
# Serial funnel: apply_subtask_status_update
# ---------------------------------------------------------------------------


async def test_serial_funnel_stamps_started_at(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    path = _write_plan(spec, "S1")

    await apply_subtask_status_update(spec, "S1", "in_progress", "", tmp_path)

    st = _subtask(path, "S1")
    assert st["status"] == "in_progress"
    assert st["started_at"], "the value the cockpit reads must actually arrive"
    assert datetime.fromisoformat(st["started_at"]).tzinfo is not None


async def test_serial_funnel_does_not_reset_started_at_on_retry(
    tmp_path: Path,
) -> None:
    """A redone subtask keeps its original start, so the cockpit's timer does
    not jump backwards mid-build."""
    spec = tmp_path / "spec"
    path = _write_plan(spec, "S1")

    await apply_subtask_status_update(spec, "S1", "in_progress", "", tmp_path)
    first = _subtask(path, "S1")["started_at"]
    await apply_subtask_status_update(spec, "S1", "failed", "", tmp_path)
    await apply_subtask_status_update(spec, "S1", "in_progress", "", tmp_path)

    assert _subtask(path, "S1")["started_at"] == first


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_serial_funnel_does_not_stamp_other_statuses(
    tmp_path: Path, status: str
) -> None:
    spec = tmp_path / "spec"
    path = _write_plan(spec, "S1")

    await apply_subtask_status_update(spec, "S1", status, "", tmp_path)

    assert "started_at" not in _subtask(path, "S1")


# ---------------------------------------------------------------------------
# Wave funnel: record_subtask_started
# ---------------------------------------------------------------------------


def test_wave_funnel_stamps_the_whole_wave(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    path = _write_plan(spec, "A", "B", "C")

    stamped = record_subtask_started(["A", "B"], path, None)

    assert stamped == 2
    assert _subtask(path, "A")["started_at"]
    assert _subtask(path, "B")["started_at"]
    # C was not in this wave and must not be stamped.
    assert "started_at" not in _subtask(path, "C")


def test_wave_funnel_falls_back_to_the_canonical_source_plan(
    tmp_path: Path,
) -> None:
    """Same fallback record_subtask_completion has: the worktree spec dir often
    holds no plan, and losing the write there is how the wave path went blank."""
    source = tmp_path / "source"
    path = _write_plan(source, "A")
    missing = tmp_path / "worktree" / "implementation_plan.json"

    assert record_subtask_started(["A"], missing, source) == 1
    assert _subtask(path, "A")["started_at"]


def test_wave_funnel_is_inert_with_no_plan(tmp_path: Path) -> None:
    """Bookkeeping must never raise -- a build cannot fail on a timestamp."""
    assert record_subtask_started(["A"], tmp_path / "nope.json", None) == 0


def test_wave_funnel_does_not_reset_an_existing_start(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    path = _write_plan(spec, "A")

    record_subtask_started(["A"], path, None)
    first = _subtask(path, "A")["started_at"]
    record_subtask_started(["A"], path, None)

    assert _subtask(path, "A")["started_at"] == first


# ---------------------------------------------------------------------------
# The two timestamps must share one timezone frame
# ---------------------------------------------------------------------------


def test_started_and_completed_are_both_aware_utc(tmp_path: Path) -> None:
    """The cockpit does ``Date.parse(completed_at) - Date.parse(started_at)``.
    A naive local stamp on either side skews that by the UTC offset -- which is
    silent, and wrong by an hour for half the year in BST."""
    spec = tmp_path / "spec"
    path = _write_plan(spec, "A")

    record_subtask_started(["A"], path, None)
    plan = ImplementationPlan.load(path)
    subtask = plan.phases[0].subtasks[0]
    subtask.complete()
    plan.save(path)

    st = _subtask(path, "A")
    started = datetime.fromisoformat(st["started_at"])
    completed = datetime.fromisoformat(st["completed_at"])
    assert started.tzinfo is not None
    assert completed.tzinfo is not None
    assert (completed - started).total_seconds() >= 0


def test_session_id_is_gone_from_the_serialized_plan(tmp_path: Path) -> None:
    """It had no reader in any of the six fleet repos and only a writer nothing
    called, so it could only ever serialize as absent (#1195)."""
    spec = tmp_path / "spec"
    path = _write_plan(spec, "A")
    record_subtask_started(["A"], path, None)

    assert "session_id" not in _subtask(path, "A")
    assert not hasattr(Subtask(id="x", description="y"), "session_id")
