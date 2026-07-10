#!/usr/bin/env python3
"""
Tests for the parallel phase runner (#376)
==========================================

Exercises the concurrency-control flow deterministically with injected fakes:
wave sequencing, the "coding concurrent / merge sequential / plan-write
sequential" invariants, dependency gating, partial-failure handling, and stall
detection. No real worktrees or agent sessions are involved.
"""

import asyncio

import pytest
from agents.parallel_runner import (
    PhaseRunResult,
    SubtaskResult,
    is_phase_parallel_eligible,
    run_parallel_phase,
)
from implementation_plan.enums import SubtaskStatus
from implementation_plan.phase import Phase
from implementation_plan.subtask import Subtask

# asyncio_mode=auto (see pytest.ini) auto-detects async tests; no marker needed.


def _st(id, *, deps=None, create=None, modify=None, status=SubtaskStatus.PENDING):
    return Subtask(
        id=id,
        description=id,
        status=status,
        depends_on=deps or [],
        files_to_create=create or [],
        files_to_modify=modify or [],
    )


class _Harness:
    """Records call order/concurrency for the three injectable hooks."""

    def __init__(self, *, fail_run=None, fail_merge=None, raise_run=None):
        self.fail_run = set(fail_run or [])
        self.fail_merge = set(fail_merge or [])
        self.raise_run = set(raise_run or [])
        self.run_order: list[str] = []
        self.merge_order: list[str] = []
        self.complete_order: list[str] = []
        self._running = 0
        self.max_concurrent_run = 0
        self._merging = 0
        self.max_concurrent_merge = 0

    async def run_subtask(self, subtask, *, index):
        self._running += 1
        self.max_concurrent_run = max(self.max_concurrent_run, self._running)
        await asyncio.sleep(0.01)  # force overlap if concurrent
        self.run_order.append(subtask.id)
        self._running -= 1
        if subtask.id in self.raise_run:
            raise RuntimeError(f"boom-{subtask.id}")
        success = subtask.id not in self.fail_run
        return SubtaskResult(
            subtask_id=subtask.id,
            success=success,
            worktree_name=f"wt-{subtask.id}",
            error=None if success else "failed",
        )

    async def merge_subtask(self, subtask, result):
        self._merging += 1
        self.max_concurrent_merge = max(self.max_concurrent_merge, self._merging)
        await asyncio.sleep(0.005)
        self.merge_order.append(subtask.id)
        self._merging -= 1
        return subtask.id not in self.fail_merge

    async def mark_complete(self, subtask):
        self.complete_order.append(subtask.id)

    def kwargs(self):
        return {
            "run_subtask": self.run_subtask,
            "merge_subtask": self.merge_subtask,
            "mark_complete": self.mark_complete,
        }


class TestHappyPath:
    async def test_independent_subtasks_one_wave_all_complete(self):
        tasks = [_st(x, create=[f"{x}.py"]) for x in ("a", "b", "c")]
        h = _Harness()
        res = await run_parallel_phase(tasks, workers=3, **h.kwargs())
        assert res.ok
        assert res.completed_ids == ["a", "b", "c"]
        assert res.waves == 1
        assert all(t.status == SubtaskStatus.COMPLETED for t in tasks)

    async def test_coding_runs_concurrently(self):
        tasks = [_st(x, create=[f"{x}.py"]) for x in ("a", "b", "c")]
        h = _Harness()
        await run_parallel_phase(tasks, workers=3, **h.kwargs())
        # All three coded at once.
        assert h.max_concurrent_run == 3

    async def test_merges_are_sequential(self):
        tasks = [_st(x, create=[f"{x}.py"]) for x in ("a", "b", "c")]
        h = _Harness()
        await run_parallel_phase(tasks, workers=3, **h.kwargs())
        # Invariant #2: never two merges at once.
        assert h.max_concurrent_merge == 1
        assert h.merge_order == h.complete_order  # complete follows each merge


class TestWavesAndDeps:
    async def test_worker_cap_splits_into_waves(self):
        tasks = [_st(x, create=[f"{x}.py"]) for x in ("a", "b", "c", "d")]
        h = _Harness()
        res = await run_parallel_phase(tasks, workers=2, **h.kwargs())
        assert res.ok
        assert res.waves == 2  # 4 tasks / 2 workers
        assert sorted(res.completed_ids) == ["a", "b", "c", "d"]

    async def test_dependencies_force_ordering(self):
        # c depends on a; b independent.
        tasks = [
            _st("a", create=["a.py"]),
            _st("b", create=["b.py"]),
            _st("c", deps=["a"], create=["c.py"]),
        ]
        h = _Harness()
        res = await run_parallel_phase(tasks, workers=3, **h.kwargs())
        assert res.ok
        # a must complete before c starts.
        assert h.run_order.index("a") < h.run_order.index("c")
        assert h.complete_order.index("a") < h.complete_order.index("c")

    async def test_conflicting_files_run_in_separate_waves(self):
        tasks = [
            _st("a", create=["shared.py"]),
            _st("b", modify=["shared.py"]),
        ]
        h = _Harness()
        res = await run_parallel_phase(tasks, workers=4, **h.kwargs())
        assert res.ok
        assert res.waves == 2  # cannot co-schedule conflicting files


class TestFailures:
    async def test_session_failure_marks_failed_others_succeed(self):
        tasks = [_st(x, create=[f"{x}.py"]) for x in ("a", "b", "c")]
        h = _Harness(fail_run=["b"])
        res = await run_parallel_phase(tasks, workers=3, **h.kwargs())
        assert "b" in res.failed_ids
        assert set(res.completed_ids) == {"a", "c"}
        assert not res.ok
        # Failed subtask is never merged.
        assert "b" not in h.merge_order

    async def test_merge_failure_marks_failed(self):
        tasks = [_st("a", create=["a.py"])]
        h = _Harness(fail_merge=["a"])
        res = await run_parallel_phase(tasks, workers=2, **h.kwargs())
        assert res.failed_ids == ["a"]
        assert "a" not in h.complete_order

    async def test_exception_in_run_is_contained(self):
        tasks = [_st("a", create=["a.py"]), _st("b", create=["b.py"])]
        h = _Harness(raise_run=["a"])
        res = await run_parallel_phase(tasks, workers=2, **h.kwargs())
        assert "a" in res.failed_ids
        assert "b" in res.completed_ids

    async def test_dependent_blocked_when_prereq_fails(self):
        # b depends on a; a fails -> b can never become ready -> stall.
        tasks = [_st("a", create=["a.py"]), _st("b", deps=["a"], create=["b.py"])]
        h = _Harness(fail_run=["a"])
        res = await run_parallel_phase(tasks, workers=3, **h.kwargs())
        assert "a" in res.failed_ids
        assert res.stalled
        assert "b" in res.remaining_ids


class TestStall:
    async def test_cycle_stalls_immediately(self):
        tasks = [
            _st("a", deps=["b"], create=["a.py"]),
            _st("b", deps=["a"], create=["b.py"]),
        ]
        h = _Harness()
        res = await run_parallel_phase(tasks, workers=3, **h.kwargs())
        assert res.stalled
        assert set(res.remaining_ids) == {"a", "b"}
        assert res.completed_ids == []

    async def test_already_completed_are_skipped(self):
        tasks = [
            _st("a", create=["a.py"], status=SubtaskStatus.COMPLETED),
            _st("b", create=["b.py"]),
        ]
        h = _Harness()
        res = await run_parallel_phase(tasks, workers=3, **h.kwargs())
        assert res.completed_ids == ["a", "b"]
        assert h.run_order == ["b"]  # a not re-run


class TestEligibility:
    def test_requires_two_pending(self):
        ph = Phase(
            phase=1,
            name="p",
            subtasks=[
                _st("a", create=["a.py"], status=SubtaskStatus.COMPLETED),
                _st("b", create=["b.py"]),
            ],
            parallel_safe=True,
        )
        assert not is_phase_parallel_eligible(ph, workers=3)

    def test_eligible_when_safe_and_multiple_pending(self):
        # parallel_safe overrides even when file sets are unknown/empty.
        ph = Phase(phase=1, name="p", subtasks=[_st("a"), _st("b")], parallel_safe=True)
        assert is_phase_parallel_eligible(ph, workers=3)

    def test_not_eligible_with_one_worker(self):
        ph = Phase(
            phase=1,
            name="p",
            subtasks=[_st("a", create=["a.py"]), _st("b", create=["b.py"])],
            parallel_safe=True,
        )
        assert not is_phase_parallel_eligible(ph, workers=1)

    # --- auto-derive (#376): planner omitted parallel_safe ---
    def test_autoderive_eligible_from_disjoint_files(self):
        ph = Phase(
            phase=1,
            name="p",
            parallel_safe=False,
            subtasks=[_st("a", create=["app/x.py"]), _st("b", create=["tests/x.py"])],
        )
        assert is_phase_parallel_eligible(ph, workers=3)

    def test_autoderive_not_eligible_when_files_overlap(self):
        ph = Phase(
            phase=1,
            name="p",
            parallel_safe=False,
            subtasks=[_st("a", modify=["app/x.py"]), _st("b", modify=["app/x.py"])],
        )
        assert not is_phase_parallel_eligible(ph, workers=3)

    def test_autoderive_not_eligible_when_footprint_unknown(self):
        # Empty file sets => unknown scope => cannot prove safe => not eligible.
        ph = Phase(
            phase=1, name="p", parallel_safe=False, subtasks=[_st("a"), _st("b")]
        )
        assert not is_phase_parallel_eligible(ph, workers=3)

    def test_autoderive_not_eligible_when_dependent(self):
        ph = Phase(
            phase=1,
            name="p",
            parallel_safe=False,
            subtasks=[_st("a", create=["a.py"]), _st("b", deps=["a"], create=["b.py"])],
        )
        assert not is_phase_parallel_eligible(ph, workers=3)

    def test_autoderive_eligible_with_third_independent(self):
        ph = Phase(
            phase=1,
            name="p",
            parallel_safe=False,
            subtasks=[
                _st("a", create=["a.py"]),
                _st("b", deps=["a"], create=["b.py"]),
                _st("c", create=["c.py"]),
            ],
        )
        assert is_phase_parallel_eligible(ph, workers=3)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
