#!/usr/bin/env python3
"""Test evidence is scoped to the subtask, not the build (#1187).

The #851 honesty gate asks "did a real test command run". It read
``read_test_evidence(spec_dir)`` — a summary of every run recorded for the whole
build — so the SECOND verification subtask in a build inherited the first one's
green ``pytest -q`` and completed having executed nothing. Live shape: a subtask
whose only declared deliverable was ``docs/plans/030-testing-strategy.md``
shipped a document and completed green.

``test_the_ride`` is the control: it reproduces that, so the hole is a fact in
the repo rather than a claim in a commit message. Everything after it asserts
the fix, and — the point of a gate that will be kept switched on — that the
honest cases it must not touch still complete: a verification subtask that ran
its own suite, an ordinary subtask that correctly runs no tests at all, and an
evidence file written before this change.

Refs #851, #1178, #1191.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents.parallel_integration import gated_mark_complete  # noqa: E402
from agents.test_evidence import (  # noqa: E402
    read_test_evidence,
    record_subtask_completed,
    record_test_run,
)
from agents.tools_pkg.tools.subtask import (  # noqa: E402
    apply_subtask_status_update,
)

# An ordinary feature subtask. It runs the suite as part of its own work, which
# is normal and honest — the run is simply not the NEXT subtask's evidence.
FEATURE: dict[str, Any] = {
    "id": "1.1",
    "description": "Add the VAT quote calculation module",
    "files_to_create": ["app/vat_quote.py"],
    "status": "pending",
}
# The live #1187 shape: a verification subtask whose only deliverable is prose.
# #1191's `testing` gate is keyed on ``service == "testing"``; this one carries
# no service, so only #851 governs it — which is the gate under test.
VERIFY: dict[str, Any] = {
    "id": "1.2",
    "description": "Run the full unit test suite and verify the endpoint",
    "files_to_create": ["docs/plans/030-testing-strategy.md"],
    "status": "pending",
}
# The case the gate must never touch: no test language anywhere in it.
PLAIN: dict[str, Any] = {
    "id": "1.3",
    "description": "Add a slugify() helper to strutil",
    "files_to_create": ["app/strutil.py"],
    "status": "pending",
}


def _spec(root: Path, subtasks: list[dict[str, Any]]) -> Path:
    spec = root / ".aifactory" / "specs" / "030-vat"
    spec.mkdir(parents=True)
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {"phases": [{"name": "impl", "subtasks": [dict(s) for s in subtasks]}]}
        )
    )
    return spec


async def _complete(spec: Path, sid: str, project: Path) -> str:
    out = await apply_subtask_status_update(spec, sid, "completed", project_dir=project)
    return str(out["content"][0]["text"])


def _status(spec: Path, sid: str) -> str:
    plan = json.loads((spec / "implementation_plan.json").read_text())
    return next(s["status"] for s in plan["phases"][0]["subtasks"] if s["id"] == sid)


# ── the ride, demonstrated ───────────────────────────────────────────────────


def _build_wide(spec_dir: Path, subtask_id: str | None = None) -> dict[str, Any]:
    """``read_test_evidence`` exactly as it read before #1187: every run recorded
    anywhere in the build, whichever subtask made it."""
    runs: list[dict[str, Any]] = []
    path = Path(spec_dir) / ".aifactory" / "test_evidence.jsonl"
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            entry = json.loads(raw)
            if "command" in entry:
                runs.append(entry)
    last = runs[-1] if runs else None
    return {
        "ran": bool(runs),
        "last_failed": bool(last and last.get("failed")),
        "runs": len(runs),
        "last_command": last.get("command") if last else None,
    }


async def test_the_ride(tmp_path, monkeypatch):
    """Pre-#1187 reading: build-wide evidence completes the prose-only subtask.

    Driven through the REAL serial completion tool, with only the evidence
    reader put back to its build-wide form — which is the whole of the defect.
    """
    import agents.test_evidence as ev_mod

    monkeypatch.setattr(ev_mod, "read_test_evidence", _build_wide)

    spec = _spec(tmp_path, [FEATURE, VERIFY])

    record_test_run(spec, "pytest -q", "collected 3 items ... 3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)

    assert _build_wide(spec) == {
        "ran": True,
        "last_failed": False,
        "runs": 1,
        "last_command": "pytest -q",
    }
    assert "Successfully" in await _complete(spec, "1.2", tmp_path)
    assert _status(spec, "1.2") == "completed"  # shipped a document, ran nothing


# ── the fix ──────────────────────────────────────────────────────────────────


async def test_a_verification_subtask_may_not_ride_an_earlier_subtasks_run(tmp_path):
    spec = _spec(tmp_path, [FEATURE, VERIFY])

    record_test_run(spec, "pytest -q", "collected 3 items ... 3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)

    refusal = await _complete(spec, "1.2", tmp_path)
    assert "Refused" in refusal
    assert "does not count for this one" in refusal
    assert _status(spec, "1.2") == "pending"  # the plan is left untouched


async def test_a_verification_subtask_that_ran_its_own_suite_completes(tmp_path):
    """A gate that refuses everything is not a fix."""
    spec = _spec(tmp_path, [FEATURE, VERIFY])

    record_test_run(spec, "pytest -q", "collected 3 items ... 3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)

    record_test_run(spec, "pytest -q", "collected 9 items ... 9 passed")
    assert "Successfully" in await _complete(spec, "1.2", tmp_path)
    assert _status(spec, "1.2") == "completed"


async def test_a_subtask_that_correctly_runs_no_tests_is_untouched(tmp_path):
    """#851 only ever governs verification subtasks. Narrowing the window does
    not widen the gate: a subtask with no test language completes with an empty
    ledger, exactly as before."""
    spec = _spec(tmp_path, [FEATURE, VERIFY, PLAIN])

    record_test_run(spec, "pytest -q", "3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)
    assert "Successfully" in await _complete(spec, "1.3", tmp_path)
    assert _status(spec, "1.3") == "completed"


async def test_a_refusal_does_not_consume_the_runs_the_coder_still_owes(tmp_path):
    """The window closes on an ACCEPTED completion only, so the retry the
    refusal asks for actually works."""
    spec = _spec(tmp_path, [FEATURE, VERIFY])

    record_test_run(spec, "pytest -q", "3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)
    assert "Refused" in await _complete(spec, "1.2", tmp_path)

    record_test_run(spec, "pytest -q", "9 passed")
    assert "Successfully" in await _complete(spec, "1.2", tmp_path)


async def test_re_reporting_the_same_subtask_is_not_refused_for_its_own_run(tmp_path):
    """A completion marker for THIS subtask is its own earlier attempt, not a
    boundary — otherwise a repeated tool call turns an honest completion into a
    refusal on the second try."""
    spec = _spec(tmp_path, [FEATURE, VERIFY])

    record_test_run(spec, "pytest -q", "3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)
    record_test_run(spec, "pytest -q", "9 passed")
    assert "Successfully" in await _complete(spec, "1.2", tmp_path)

    assert "Successfully" in await _complete(spec, "1.2", tmp_path)


async def test_a_failing_run_inside_the_window_is_still_refused(tmp_path):
    """The other half of #851 keeps working within the narrowed window."""
    spec = _spec(tmp_path, [FEATURE, VERIFY])

    record_test_run(spec, "pytest -q", "3 passed")
    assert "Successfully" in await _complete(spec, "1.1", tmp_path)
    record_test_run(spec, "pytest -q", "=== 2 failed, 7 passed ===")

    refusal = await _complete(spec, "1.2", tmp_path)
    assert "the last recorded test run failed" in refusal


# ── backward compatibility: nothing already on disk is retroactively refused ──


def test_an_evidence_file_written_before_this_change_reads_as_it_always_did(tmp_path):
    """Every ledger line on disk today is a run with no completion marker, so
    the whole file is the window — the pre-#1187 build-wide reading."""
    ledger = tmp_path / ".aifactory" / "test_evidence.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"ts": 1.0, "command": "pytest -q", "failed": false}\n'
        '{"ts": 2.0, "command": "pytest -q", "failed": false}\n'
    )

    assert read_test_evidence(tmp_path, "1.2") == {
        "ran": True,
        "last_failed": False,
        "runs": 2,
        "last_command": "pytest -q",
    }


def test_an_unrecognised_ledger_line_does_not_crash_or_close_the_window(tmp_path):
    """A shape this module has never seen is counted as a run, not an error: a
    gate that raises on unfamiliar evidence fails a build for a reason that says
    nothing about the code."""
    ledger = tmp_path / ".aifactory" / "test_evidence.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"ts": 1.0, "command": "pytest -q", "failed": false}\n'
        '{"ts": 2.0, "note": "written by a future version"}\n'
    )

    assert read_test_evidence(tmp_path, "1.2")["ran"] is True


def test_the_window_boundary_is_the_last_completion_of_another_subtask(tmp_path):
    """The reader itself, without either engine around it."""
    record_test_run(tmp_path, "pytest -q", "3 passed")
    assert read_test_evidence(tmp_path, "1.2")["ran"]  # no boundary yet

    record_subtask_completed(tmp_path, "1.1")
    assert read_test_evidence(tmp_path, "1.2")["ran"] is False
    assert read_test_evidence(tmp_path, "1.1")["ran"] is True

    record_test_run(tmp_path, "go test ./...", "ok")
    assert read_test_evidence(tmp_path, "1.2") == {
        "ran": True,
        "last_failed": False,
        "runs": 1,
        "last_command": "go test ./...",
    }


# ── the two engines mean the same thing by "a test ran" ──────────────────────


async def test_a_wave_completion_closes_the_window_the_serial_engine_reads(tmp_path):
    """Cross-engine, behavioural: the wave path completes 1.1 off a recorded
    run, and the serial path then refuses 1.2 for riding it.

    Before #1187 the two engines disagreed — the wave was already per-subtask
    (a child records into its own worktree spec dir, #1178) while the serial
    path was build-wide, so "a test ran" meant different things depending on
    which path a build took.
    """
    spec = _spec(tmp_path, [FEATURE, VERIFY])
    record_test_run(spec, "pytest -q", "collected 3 items ... 3 passed")

    accepted = await gated_mark_complete(
        _Subtask("1.1", FEATURE),
        plan_path=spec / "implementation_plan.json",
        source_spec_dir=None,
        project_dir=tmp_path,
        evidence=read_test_evidence(spec, "1.1"),
    )
    assert accepted is True

    refusal = await _complete(spec, "1.2", tmp_path)
    assert "does not count for this one" in refusal


async def test_a_serial_completion_closes_the_window_the_wave_engine_reads(tmp_path):
    """...and the same in the other direction."""
    spec = _spec(tmp_path, [FEATURE, VERIFY])
    record_test_run(spec, "pytest -q", "collected 3 items ... 3 passed")

    assert "Successfully" in await _complete(spec, "1.1", tmp_path)

    accepted = await gated_mark_complete(
        _Subtask("1.2", VERIFY),
        plan_path=spec / "implementation_plan.json",
        source_spec_dir=None,
        project_dir=tmp_path,
        evidence=read_test_evidence(spec, "1.2"),
    )
    assert accepted is False


def test_neither_engine_reads_the_evidence_build_wide():
    """Structural, in the spirit of ``test_both_engines_call_the_same_gate``:
    a ``read_test_evidence(spec_dir)`` with no subtask id anywhere in either
    engine is the defect returning on one path only."""
    backend = Path(__file__).resolve().parents[1] / "apps" / "backend" / "agents"
    engines = [
        backend / "tools_pkg" / "tools" / "subtask.py",
        backend / "parallel_integration.py",
    ]

    calls = 0
    for path in engines:
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "read_test_evidence"
            ):
                calls += 1
                assert len(node.args) == 2, (
                    f"{path.name}:{node.lineno} reads test evidence build-wide"
                )
    assert calls >= 3  # serial x1, wave child capture + wave fallback


class _Subtask:
    """The minimum a wave child is: an id and a ``to_dict()`` the gates read."""

    def __init__(self, sid: str, record: dict[str, Any]) -> None:
        self.id = sid
        self._record = dict(record) | {"id": sid}

    def to_dict(self) -> dict[str, Any]:
        return dict(self._record)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
