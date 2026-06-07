#!/usr/bin/env python3
"""
Tests for the wave scheduler (#376)
===================================

Covers the pure scheduling logic that decides which independent subtasks may
run concurrently: dependency readiness, file-set conflict avoidance, worker
caps, and DAG validation (unknown deps, self-deps, cycles).
"""

import pytest
from implementation_plan.enums import SubtaskStatus
from implementation_plan.scheduler import (
    conflicts,
    file_set,
    select_ready_wave,
    validate_dependencies,
)
from implementation_plan.story import Story
from implementation_plan.subtask import Subtask


def _st(id, *, deps=None, create=None, modify=None, status=SubtaskStatus.PENDING):
    return Subtask(
        id=id,
        description=id,
        status=status,
        depends_on=deps or [],
        files_to_create=create or [],
        files_to_modify=modify or [],
    )


class TestFileSet:
    def test_union_and_normalization(self):
        s = _st("a", create=["./app/config.py"], modify=["app/main.py "])
        assert file_set(s) == {"app/config.py", "app/main.py"}

    def test_empty_when_no_files(self):
        assert file_set(_st("a")) == set()

    def test_works_on_story(self):
        story = Story(id="US-1", title="t", user_story="u", files_to_create=["x.py"])
        assert file_set(story) == {"x.py"}


class TestConflicts:
    def test_disjoint_do_not_conflict(self):
        assert not conflicts(_st("a", create=["a.py"]), _st("b", create=["b.py"]))

    def test_overlap_conflicts(self):
        assert conflicts(_st("a", create=["x.py"]), _st("b", modify=["x.py"]))

    def test_unknown_footprint_conflicts_with_everything(self):
        # Empty file set => unknown scope => must run solo.
        assert conflicts(_st("a"), _st("b", create=["b.py"]))
        assert conflicts(_st("a", create=["a.py"]), _st("b"))


class TestSelectReadyWave:
    def test_independent_subtasks_fill_workers(self):
        tasks = [
            _st("a", create=["a.py"]),
            _st("b", create=["b.py"]),
            _st("c", create=["c.py"]),
        ]
        wave = select_ready_wave(tasks, completed_ids=set(), max_workers=4)
        assert [t.id for t in wave] == ["a", "b", "c"]

    def test_worker_cap_respected(self):
        tasks = [_st(x, create=[f"{x}.py"]) for x in ("a", "b", "c", "d")]
        wave = select_ready_wave(tasks, completed_ids=set(), max_workers=2)
        assert [t.id for t in wave] == ["a", "b"]

    def test_conflicting_files_not_co_scheduled(self):
        tasks = [
            _st("a", create=["shared.py"]),
            _st("b", modify=["shared.py"]),
            _st("c", create=["c.py"]),
        ]
        wave = select_ready_wave(tasks, completed_ids=set(), max_workers=4)
        # b conflicts with a (same file); c is independent.
        assert [t.id for t in wave] == ["a", "c"]

    def test_dependencies_gate_readiness(self):
        tasks = [
            _st("a", create=["a.py"]),
            _st("b", deps=["a"], create=["b.py"]),
        ]
        wave = select_ready_wave(tasks, completed_ids=set(), max_workers=4)
        assert [t.id for t in wave] == ["a"]  # b waits for a

        wave2 = select_ready_wave(tasks, completed_ids={"a"}, max_workers=4)
        assert [t.id for t in wave2] == ["b"]

    def test_in_flight_counts_against_workers_and_reserves_files(self):
        running = _st("a", create=["shared.py"], status=SubtaskStatus.IN_PROGRESS)
        tasks = [
            _st("b", create=["shared.py"]),  # conflicts with running a
            _st("c", create=["c.py"]),
        ]
        wave = select_ready_wave(
            tasks, completed_ids=set(), in_flight=[running], max_workers=2
        )
        # b blocked by file reservation, c ok; 1 free slot (2 - 1 in-flight).
        assert [t.id for t in wave] == ["c"]

    def test_no_free_slots_returns_empty(self):
        running = [_st("a", create=["a.py"], status=SubtaskStatus.IN_PROGRESS)]
        tasks = [_st("b", create=["b.py"])]
        assert select_ready_wave(tasks, set(), in_flight=running, max_workers=1) == []

    def test_unknown_footprint_runs_solo(self):
        tasks = [
            _st("a"),  # unknown scope
            _st("b", create=["b.py"]),
        ]
        wave = select_ready_wave(tasks, completed_ids=set(), max_workers=4)
        assert [t.id for t in wave] == ["a"]  # a reserved alone, blocks b

    def test_completed_and_nonpending_skipped(self):
        tasks = [
            _st("a", create=["a.py"], status=SubtaskStatus.COMPLETED),
            _st("b", create=["b.py"], status=SubtaskStatus.FAILED),
            _st("c", create=["c.py"]),
        ]
        wave = select_ready_wave(tasks, completed_ids={"a"}, max_workers=4)
        assert [t.id for t in wave] == ["c"]

    def test_workers_floor_of_one(self):
        tasks = [_st("a", create=["a.py"]), _st("b", create=["b.py"])]
        wave = select_ready_wave(tasks, set(), max_workers=0)
        assert [t.id for t in wave] == ["a"]


class TestValidateDependencies:
    def test_valid_dag_has_no_errors(self):
        tasks = [_st("a"), _st("b", deps=["a"]), _st("c", deps=["a", "b"])]
        assert validate_dependencies(tasks) == []

    def test_unknown_dependency_flagged(self):
        errors = validate_dependencies([_st("a", deps=["ghost"])])
        assert any("unknown subtask 'ghost'" in e for e in errors)

    def test_self_dependency_flagged(self):
        errors = validate_dependencies([_st("a", deps=["a"])])
        assert any("depends on itself" in e for e in errors)

    def test_cycle_detected(self):
        tasks = [_st("a", deps=["b"]), _st("b", deps=["a"])]
        errors = validate_dependencies(tasks)
        assert any("cycle" in e.lower() for e in errors)

    def test_longer_cycle_detected(self):
        tasks = [_st("a", deps=["c"]), _st("b", deps=["a"]), _st("c", deps=["b"])]
        errors = validate_dependencies(tasks)
        assert any("cycle" in e.lower() for e in errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
