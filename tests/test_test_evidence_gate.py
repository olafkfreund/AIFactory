"""#851: the honest-verification gate — a test/verification subtask cannot be
marked ``completed`` unless a real test command actually ran this build.

The Dishonest Coder wrote ``[x] Run all tests`` for a repo with no toolchain to
run them. These tests pin the gate that makes that claim falsifiable.
"""

import json
from pathlib import Path

import pytest
from agents.test_evidence import (
    is_test_command,
    is_verification_subtask,
    looks_failed,
    read_test_evidence,
    record_test_run,
)
from agents.tools_pkg.tools.subtask import apply_subtask_status_update

_PLAN = {
    "phases": [
        {
            "id": "p1",
            "name": "Implement",
            "subtasks": [
                {
                    "id": "1.1",
                    "title": "Create strutil module",
                    "status": "in_progress",
                },
                {"id": "4.1", "title": "Run all tests", "status": "in_progress"},
            ],
        }
    ]
}


def _spec(tmp_path: Path) -> Path:
    (tmp_path / "implementation_plan.json").write_text(json.dumps(_PLAN))
    return tmp_path


def _complete(spec_dir: Path, subtask_id: str):
    return apply_subtask_status_update(spec_dir, subtask_id, "completed")


def _status(spec_dir: Path, subtask_id: str) -> str:
    plan = json.loads((spec_dir / "implementation_plan.json").read_text())
    for phase in plan["phases"]:
        for st in phase["subtasks"]:
            if st["id"] == subtask_id:
                return st["status"]
    raise AssertionError("subtask not found")


# -- unit: command / subtask / failure classification --------------------------


def test_is_test_command_runs_vs_mentions():
    assert is_test_command("pytest -q")
    assert is_test_command("cd api && go test ./...")
    assert is_test_command("pip install pytest && pytest")  # install THEN run
    assert not is_test_command("pip install pytest")
    assert not is_test_command("cat tests/test_foo.py")
    assert not is_test_command("")


def test_is_verification_subtask():
    assert is_verification_subtask({"title": "Run all tests"})
    assert is_verification_subtask({"description": "verify the implementation"})
    assert not is_verification_subtask({"title": "Create the strutil module"})


def test_looks_failed_only_on_clear_markers():
    assert looks_failed("=== 2 failed, 1 passed ===")
    assert not looks_failed("=== 5 passed in 0.1s ===")
    assert not looks_failed("")  # ambiguous/empty is NOT a failure → never false-block


# -- the gate ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_subtask_refused_without_evidence(tmp_path):
    """The #851 bug, pinned: no test ran → completing 'Run all tests' is refused
    and the plan status is left unchanged."""
    spec = _spec(tmp_path)
    res = await _complete(spec, "4.1")
    assert "Refused" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "in_progress", (
        "status must NOT be persisted on refusal"
    )


@pytest.mark.asyncio
async def test_test_subtask_allowed_with_passing_evidence(tmp_path):
    spec = _spec(tmp_path)
    record_test_run(spec, "pytest -q", "=== 5 passed in 0.1s ===")
    res = await _complete(spec, "4.1")
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "completed"


@pytest.mark.asyncio
async def test_test_subtask_refused_when_last_run_failed(tmp_path):
    spec = _spec(tmp_path)
    record_test_run(spec, "pytest -q", "=== 1 failed, 4 passed ===")
    res = await _complete(spec, "4.1")
    assert "Refused" in res["content"][0]["text"]
    assert "last recorded test run failed" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "in_progress"


@pytest.mark.asyncio
async def test_non_test_subtask_always_allowed(tmp_path):
    """A normal implementation subtask is never gated — no evidence needed."""
    spec = _spec(tmp_path)
    res = await _complete(spec, "1.1")
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "1.1") == "completed"


@pytest.mark.asyncio
async def test_gate_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    spec = _spec(tmp_path)
    res = await _complete(spec, "4.1")  # no evidence, but gate off
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "completed"


def test_read_evidence_last_failed_reflects_only_latest(tmp_path):
    """A coder that fixed a failure and re-ran green is not held to the earlier
    failure — last_failed tracks only the most recent run."""
    record_test_run(tmp_path, "pytest", "1 failed")
    record_test_run(tmp_path, "pytest", "5 passed")
    ev = read_test_evidence(tmp_path)
    assert ev["ran"] and ev["runs"] == 2 and not ev["last_failed"]
