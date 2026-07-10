#!/usr/bin/env python3
"""Tests for post-compact context recovery (#262).

Covers:
- the re-injection primitive (operational context block content)
- best-effort compaction detection (input-token drop heuristic)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.compaction_recovery import (  # noqa: E402
    CompactionDetector,
    build_operational_context,
)


def _write_plan(spec_dir: Path) -> None:
    plan = {
        "phases": [
            {
                "subtasks": [
                    {"id": "1.1", "title": "Set up schema", "status": "completed"},
                    {
                        "id": "1.2",
                        "title": "Add auth endpoint",
                        "status": "in_progress",
                    },
                    {"id": "1.3", "title": "Wire frontend", "status": "pending"},
                ]
            }
        ]
    }
    (spec_dir / "implementation_plan.json").write_text(json.dumps(plan))


def test_operational_context_includes_subtask_and_rules(tmp_path: Path):
    spec_dir = tmp_path / "001-feature"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text(
        "# Spec\n\n## Overview\nBuild a login system with JWT.\n"
    )
    _write_plan(spec_dir)

    block = build_operational_context(spec_dir)
    assert "Operational context" in block
    assert "login system with JWT" in block
    # In-progress subtask is surfaced (preferred over pending).
    assert "1.2" in block
    assert "Add auth endpoint" in block
    # Standing coordination rules present.
    assert "current spec branch" in block


def test_operational_context_explicit_subtask_override(tmp_path: Path):
    spec_dir = tmp_path / "002-feature"
    spec_dir.mkdir()
    _write_plan(spec_dir)
    block = build_operational_context(spec_dir, current_subtask="9.9: custom task")
    assert "9.9: custom task" in block


def test_operational_context_handles_missing_files(tmp_path: Path):
    spec_dir = tmp_path / "003-bare"
    spec_dir.mkdir()
    # No spec.md, no plan — must still produce the rules block, no crash.
    block = build_operational_context(spec_dir)
    assert "Coordination rules" in block


def test_operational_context_flat_subtasks(tmp_path: Path):
    spec_dir = tmp_path / "004-flat"
    spec_dir.mkdir()
    plan = {"subtasks": [{"id": "A1", "description": "do thing", "status": "pending"}]}
    (spec_dir / "implementation_plan.json").write_text(json.dumps(plan))
    block = build_operational_context(spec_dir)
    assert "A1" in block


def test_detector_no_compaction_on_growth():
    det = CompactionDetector()
    # Monotonic growth -> never a compaction.
    assert det.observe(5_000) is False
    assert det.observe(30_000) is False
    assert det.observe(60_000) is False
    assert det.peak_input_tokens == 60_000


def test_detector_fires_on_sharp_drop():
    det = CompactionDetector(drop_ratio=0.5, min_peak_tokens=20_000)
    det.observe(50_000)  # peak grows
    det.observe(80_000)  # peak = 80k
    # Drop to 20k (< 80k * 0.5 = 40k) -> compaction.
    assert det.observe(20_000) is True
    # Peak resets to the post-compaction level.
    assert det.peak_input_tokens == 20_000


def test_detector_ignores_small_early_drop():
    det = CompactionDetector(min_peak_tokens=20_000)
    # Peak never exceeds the floor -> drops are ignored.
    det.observe(10_000)
    assert det.observe(2_000) is False


def test_detector_can_fire_again_after_regrowth():
    det = CompactionDetector(drop_ratio=0.5, min_peak_tokens=20_000)
    det.observe(80_000)
    assert det.observe(20_000) is True  # first compaction
    # Context regrows...
    det.observe(50_000)
    det.observe(90_000)
    assert det.observe(10_000) is True  # second compaction
