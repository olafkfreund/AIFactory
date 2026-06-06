#!/usr/bin/env python3
"""
Tests for parallel wave event persistence (issue #393)
======================================================

Wave markers were stdout-only, so benchmarks couldn't confirm wave engagement.
``WaveRecorder`` must persist a structured ``parallel_report.json`` and append
greppable ``[parallel]`` lines to ``build-progress.txt`` — and a serial build
(no recorder) must leave neither artifact.
"""

import json
from pathlib import Path

import pytest
from agents.wave_log import (
    PROGRESS_FILENAME,
    REPORT_FILENAME,
    WaveRecorder,
    load_parallel_report,
)


class _FakeClock:
    """Deterministic monotonic clock: each call advances by 1.0s."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


class _Result:
    def __init__(self, completed, failed=None, remaining=None, stalled=False, waves=0):
        self.completed_ids = completed
        self.failed_ids = failed or []
        self.remaining_ids = remaining or []
        self.stalled = stalled
        self.waves = waves


def _recorder(spec_dir: Path, **kw) -> WaveRecorder:
    return WaveRecorder(
        spec_dir,
        workers_max=kw.pop("workers_max", 3),
        phase_name=kw.pop("phase_name", "Endpoints"),
        clock=_FakeClock(),
        now=lambda: "2026-06-06T10:00:00",
        **kw,
    )


class TestReportPersistence:
    def test_records_waves_and_concurrency(self, tmp_path: Path):
        rec = _recorder(tmp_path)
        rec.record_wave(1, ["st1", "st2", "st3"])
        rec.record_wave(2, ["st4"])
        rec.finish(_Result(["st1", "st2", "st3", "st4"]))

        report = load_parallel_report(tmp_path)
        assert report is not None
        assert report["parallel"] is True
        assert report["workers_max"] == 3
        assert report["total_waves"] == 2
        assert report["observed_max_concurrency"] == 3  # wave 1 had 3
        assert [w["wave"] for w in report["waves"]] == [1, 2]
        assert report["waves"][0]["subtask_ids"] == ["st1", "st2", "st3"]
        assert report["waves"][1]["concurrency"] == 1
        assert report["completed"] == ["st1", "st2", "st3", "st4"]
        assert report["stalled"] is False

    def test_concurrency_capped_by_workers(self, tmp_path: Path):
        rec = _recorder(tmp_path, workers_max=2)
        rec.record_wave(1, ["a", "b", "c", "d"])  # 4 ready, only 2 workers
        rec.finish(_Result(["a", "b", "c", "d"]))
        report = load_parallel_report(tmp_path)
        assert report["waves"][0]["concurrency"] == 2
        assert report["observed_max_concurrency"] == 2

    def test_per_wave_duration_recorded(self, tmp_path: Path):
        rec = _recorder(tmp_path)
        rec.record_wave(1, ["st1"])  # start mono=1
        rec.record_wave(2, ["st2"])  # closes wave1 at mono=2 -> duration 1.0
        rec.finish(_Result(["st1", "st2"]))  # closes wave2
        report = load_parallel_report(tmp_path)
        assert report["waves"][0]["duration_s"] == 1.0
        assert "duration_s" in report["waves"][1]

    def test_live_report_includes_pending_wave(self, tmp_path: Path):
        # Before finish(), the in-flight wave still appears in the report.
        rec = _recorder(tmp_path)
        rec.record_wave(1, ["st1", "st2"])
        report = load_parallel_report(tmp_path)
        assert report["total_waves"] == 1
        assert report["waves"][0]["subtask_ids"] == ["st1", "st2"]

    def test_stalled_result_persisted(self, tmp_path: Path):
        rec = _recorder(tmp_path)
        rec.record_wave(1, ["st1", "st2"])
        rec.finish(_Result(["st1"], remaining=["st2"], stalled=True))
        report = load_parallel_report(tmp_path)
        assert report["stalled"] is True
        assert report["remaining"] == ["st2"]


class TestBuildProgressMarkers:
    def test_appends_parallel_lines(self, tmp_path: Path):
        rec = _recorder(tmp_path)
        rec.record_wave(1, ["st1", "st2"])
        rec.finish(_Result(["st1", "st2"]))

        text = (tmp_path / PROGRESS_FILENAME).read_text()
        assert "[parallel] Wave 1: 2 subtask(s)" in text
        assert "concurrency=2" in text
        assert "[parallel] Phase Endpoints complete" in text

    def test_serial_build_has_no_artifacts(self, tmp_path: Path):
        # No recorder used → neither artifact exists (serial is distinguishable).
        assert not (tmp_path / REPORT_FILENAME).exists()
        assert load_parallel_report(tmp_path) is None


class TestSourceMirroring:
    def test_writes_to_both_spec_dirs(self, tmp_path: Path):
        worktree_spec = tmp_path / "worktree" / "spec"
        main_spec = tmp_path / "main" / "spec"
        worktree_spec.mkdir(parents=True)
        main_spec.mkdir(parents=True)

        rec = WaveRecorder(
            worktree_spec,
            workers_max=2,
            phase_name="P",
            source_spec_dir=main_spec,
            clock=_FakeClock(),
            now=lambda: "T",
        )
        rec.record_wave(1, ["st1"])
        rec.finish(_Result(["st1"]))

        assert load_parallel_report(worktree_spec) is not None
        assert load_parallel_report(main_spec) is not None  # mirrored for the UI


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
