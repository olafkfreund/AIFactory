"""Tests for the structured active-task context summary (#475)."""

from __future__ import annotations

import json
from pathlib import Path

from agents.context_summary import (
    SECTIONS,
    build_active_task_summary,
    should_refresh,
    write_active_context,
)


def _seed(spec: Path):
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "requirements.json").write_text(
        json.dumps(
            {
                "title": "Add rate limiting",
                "description": "Gateway must 429 + Retry-After",
            }
        )
    )
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {
                "phases": [
                    {
                        "subtasks": [
                            {"description": "scaffold app", "status": "completed"},
                            {
                                "description": "rate limiter",
                                "status": "in_progress",
                                "files_to_modify": ["app/limit.py"],
                            },
                            {"description": "tests", "status": "pending"},
                        ]
                    }
                ]
            }
        )
    )
    (spec / "build-progress.txt").write_text("started\nwrote app/limit.py\n")


def test_summary_has_all_nine_sections(tmp_path: Path):
    _seed(tmp_path)
    s = build_active_task_summary(tmp_path)
    for sec in SECTIONS:
        assert f"## {sec}" in s, sec
    assert "Add rate limiting" in s
    assert "Gateway must 429" in s


def test_summary_reflects_plan_state(tmp_path: Path):
    _seed(tmp_path)
    s = build_active_task_summary(tmp_path)
    assert "rate limiter" in s  # current subtask
    assert "scaffold app" in s  # completed
    assert "tests" in s  # pending
    assert "app/limit.py" in s  # key file


def test_deterministic_fallback_on_empty_spec(tmp_path: Path):
    # No artifacts at all → still every section, with marked fallbacks (never empty).
    s = build_active_task_summary(tmp_path)
    for sec in SECTIONS:
        assert f"## {sec}" in s
    assert "(none recorded yet)" in s
    assert "(no recent progress log)" in s


def test_budget_truncation(tmp_path: Path):
    # A large plan so the summary exceeds the floor; then a ceiling above the
    # floor but below the content forces truncation.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "implementation_plan.json").write_text(
        json.dumps(
            {
                "phases": [
                    {
                        "subtasks": [
                            {
                                "description": f"subtask number {i} with a fairly long description line",
                                "status": "completed" if i % 2 else "pending",
                            }
                            for i in range(60)
                        ]
                    }
                ]
            }
        )
    )
    full = build_active_task_summary(tmp_path)
    assert len(full) > 800
    s = build_active_task_summary(tmp_path, budget_chars=800)
    assert len(s) < len(full)
    assert len(s) <= 800 + 40
    assert "truncated" in s


def test_anti_thrash_skips_when_recent_passes_barely_helped():
    assert should_refresh([]) is True
    assert should_refresh([0.30]) is True
    assert should_refresh([0.30, 0.02]) is True  # only the last was small
    assert should_refresh([0.05, 0.02]) is False  # last two both < 10%
    assert should_refresh([0.02, 0.40]) is True  # recovered


def test_write_active_context_persists_file(tmp_path: Path):
    _seed(tmp_path)
    p = write_active_context(tmp_path)
    assert p == tmp_path / "active_context.md"
    assert "## Active Task" in p.read_text()
