#!/usr/bin/env python3
"""
Regression: observed_max_concurrency reflects the in-flight wave (#393 follow-up)
=================================================================================

During the live benchmark, parallel_report.json showed observed_max_concurrency:0
while a 4-wide wave was running — the property counted only *closed* waves, so a
mid-run report under-reported concurrency until the wave finished. It must include
the pending wave.
"""

from pathlib import Path

import pytest
from agents.wave_log import WaveRecorder


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _rec(spec_dir: Path) -> WaveRecorder:
    return WaveRecorder(
        spec_dir, workers_max=4, phase_name="P", clock=_Clock(), now=lambda: "T"
    )


def test_in_flight_wave_counts_toward_observed_concurrency(tmp_path):
    rec = _rec(tmp_path)
    rec.record_wave(1, ["a", "b", "c", "d"])  # wave open, not closed
    assert rec.observed_max_concurrency == 4  # was 0 before the fix
    # And the persisted live report reflects it too.
    import json

    report = json.loads((tmp_path / "parallel_report.json").read_text())
    assert report["observed_max_concurrency"] == 4


def test_closed_waves_still_report_peak(tmp_path):
    rec = _rec(tmp_path)
    rec.record_wave(1, ["a", "b", "c"])  # concurrency 3
    rec.record_wave(2, ["d"])  # closes wave1; pending wave2 conc 1
    assert rec.observed_max_concurrency == 3  # max(closed=3, pending=1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
