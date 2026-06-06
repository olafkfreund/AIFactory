#!/usr/bin/env python3
"""
Tests for build_report.json profiling (Phase 1 of #397)
=======================================================

build_report aggregates the parallel wave events (#393) and token attribution
(#262) into one measurable artifact, computes speedup vs a serial baseline, and
keeps a stable schema (slots for the later self-healing phases). It must work for
both serial (no parallel report) and parallel builds.
"""

import json
from pathlib import Path

import pytest
from agents.build_report import (
    REPORT_FILENAME,
    build_report,
    compute_speedup,
    load_build_report,
    write_build_report,
)


def _write_parallel_report(spec_dir: Path) -> None:
    (spec_dir / "parallel_report.json").write_text(
        json.dumps(
            {
                "spec": spec_dir.name,
                "parallel": True,
                "workers_max": 3,
                "total_waves": 2,
                "observed_max_concurrency": 3,
                "waves": [
                    {
                        "wave": 1,
                        "subtask_ids": ["st1", "st2", "st3"],
                        "concurrency": 3,
                        "duration_s": 10.0,
                    },
                    {"wave": 2, "subtask_ids": ["st4"], "concurrency": 1, "duration_s": 5.0},
                ],
            }
        )
    )


def _write_usage(spec_dir: Path) -> None:
    (spec_dir / "token_usage.json").write_text(
        json.dumps(
            {
                "totalInputTokens": 8000,
                "outputTokens": 2000,
                "totalTokens": 10000,
                "totalCostUsd": 0.42,
                "categories": {
                    "user_messages": {"tokens": 6000, "costUsd": 0.25},
                    "coordination_context": {"tokens": 4000, "costUsd": 0.17},
                },
            }
        )
    )


class TestComputeSpeedup:
    def test_speedup_basic(self):
        assert compute_speedup(parallel_wall_s=15.0, serial_baseline_s=45.0) == 3.0

    def test_no_baseline_returns_none(self):
        assert compute_speedup(15.0, None) is None

    def test_zero_parallel_returns_none(self):
        assert compute_speedup(0.0, 45.0) is None


class TestBuildReport:
    def test_parallel_build_aggregates_waves_and_cost(self, tmp_path: Path):
        spec = tmp_path / "001-x"
        spec.mkdir()
        _write_parallel_report(spec)
        _write_usage(spec)

        report = build_report(spec, serial_baseline_s=45.0, now=lambda: "T")

        assert report["parallel"] is True
        assert report["workers_max"] == 3
        assert report["total_waves"] == 2
        assert report["observed_max_concurrency"] == 3
        assert report["parallel_wall_s"] == 15.0  # 10 + 5
        assert report["speedup_vs_serial"] == 3.0  # 45 / 15
        assert report["cost_usd"] == 0.42
        assert report["total_tokens"] == 10000
        assert report["tokens_by_category"]["user_messages"] == 6000
        assert len(report["waves"]) == 2

    def test_serial_build_has_no_parallel_section(self, tmp_path: Path):
        spec = tmp_path / "002-y"
        spec.mkdir()
        _write_usage(spec)  # cost only, no parallel_report.json

        report = build_report(spec, now=lambda: "T")
        assert report["parallel"] is False
        assert report["total_waves"] == 0
        assert report["parallel_wall_s"] == 0.0
        assert report["speedup_vs_serial"] is None
        assert report["cost_usd"] == 0.42  # cost still captured

    def test_stable_schema_slots_for_later_phases(self, tmp_path: Path):
        spec = tmp_path / "003-z"
        spec.mkdir()
        report = build_report(spec, now=lambda: "T")
        for key in (
            "qa_rounds",
            "checkpoints",
            "rollbacks",
            "self_heal_events",
            "security_findings",
            "risk_tier",
        ):
            assert key in report

    def test_extra_fields_merged(self, tmp_path: Path):
        spec = tmp_path / "004-w"
        spec.mkdir()
        report = build_report(
            spec,
            extra={"qa_rounds": 2, "risk_tier": "standard", "rollbacks": ["wave-2"]},
            now=lambda: "T",
        )
        assert report["qa_rounds"] == 2
        assert report["risk_tier"] == "standard"
        assert report["rollbacks"] == ["wave-2"]

    def test_empty_spec_does_not_crash(self, tmp_path: Path):
        spec = tmp_path / "005-empty"
        spec.mkdir()
        report = build_report(spec, now=lambda: "T")
        assert report["spec"] == "005-empty"
        assert report["cost_usd"] == 0.0


class TestPersistence:
    def test_write_and_load(self, tmp_path: Path):
        spec = tmp_path / "006-p"
        spec.mkdir()
        _write_parallel_report(spec)
        write_build_report(spec, serial_baseline_s=30.0, now=lambda: "T")

        assert (spec / REPORT_FILENAME).exists()
        loaded = load_build_report(spec)
        assert loaded is not None
        assert loaded["speedup_vs_serial"] == 2.0  # 30 / 15

    def test_mirrors_to_source_spec(self, tmp_path: Path):
        worktree = tmp_path / "wt" / "001"
        main = tmp_path / "main" / "001"
        worktree.mkdir(parents=True)
        main.mkdir(parents=True)
        _write_parallel_report(worktree)

        write_build_report(worktree, source_spec_dir=main, now=lambda: "T")
        assert load_build_report(worktree) is not None
        assert load_build_report(main) is not None  # mirrored for the UI

    def test_load_missing_returns_none(self, tmp_path: Path):
        assert load_build_report(tmp_path) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
