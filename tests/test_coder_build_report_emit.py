#!/usr/bin/env python3
"""
Integration test: build_report emission wired into the executor (Phase 7 / #397)
================================================================================

run_autonomous_agent emits build_report.json at its completion exits via
_emit_build_report. This covers that wiring unit: it writes the artifact from the
spec's existing files, mirrors to the source spec, and never raises.
"""

import json
from pathlib import Path

import pytest
from agents.coder import _emit_build_report
from agents.build_report import REPORT_FILENAME, load_build_report


def _seed_parallel_report(spec: Path) -> None:
    (spec / "parallel_report.json").write_text(
        json.dumps(
            {
                "parallel": True,
                "workers_max": 2,
                "total_waves": 1,
                "observed_max_concurrency": 2,
                "waves": [
                    {"wave": 1, "subtask_ids": ["a", "b"], "concurrency": 2, "duration_s": 7.0}
                ],
            }
        )
    )


def test_emit_writes_build_report(tmp_path: Path):
    spec = tmp_path / "001-x"
    spec.mkdir()
    _seed_parallel_report(spec)

    _emit_build_report(spec)

    assert (spec / REPORT_FILENAME).exists()
    report = load_build_report(spec)
    assert report["parallel"] is True
    assert report["total_waves"] == 1
    assert report["observed_max_concurrency"] == 2


def test_emit_mirrors_to_source_spec(tmp_path: Path):
    worktree = tmp_path / "wt" / "001"
    main = tmp_path / "main" / "001"
    worktree.mkdir(parents=True)
    main.mkdir(parents=True)
    _seed_parallel_report(worktree)

    _emit_build_report(worktree, main)
    assert load_build_report(worktree) is not None
    assert load_build_report(main) is not None  # mirrored for the UI


def test_emit_never_raises_on_bad_path(tmp_path: Path):
    # Nonexistent spec dir → swallowed, no exception (profiling is best-effort).
    _emit_build_report(tmp_path / "does-not-exist")  # must not raise


def test_serial_build_still_emits(tmp_path: Path):
    # No parallel_report.json (serial build) → still writes a report (parallel=False).
    spec = tmp_path / "002-serial"
    spec.mkdir()
    _emit_build_report(spec)
    report = load_build_report(spec)
    assert report is not None
    assert report["parallel"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
