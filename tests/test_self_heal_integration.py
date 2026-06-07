#!/usr/bin/env python3
"""
Self-heal integration layer — default-off wiring (issue #415)
=============================================================

The #397 self-healing modules are pure + tested; this covers the thin executor
wiring in agents/self_heal_integration.py. The contract: with AIFACTORY_SELF_HEAL
unset every entrypoint is a no-op (returns None), so the live loop is unchanged;
with it enabled, the helpers run verify/checkpoint/security/artifacts.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents import self_heal_integration as shi  # noqa: E402


def _enable(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SELF_HEAL", "1")


# ---- flag ----------------------------------------------------------------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("AIFACTORY_SELF_HEAL", raising=False)
    assert shi.is_self_heal_enabled() is False


@pytest.mark.parametrize("val,expected", [("1", True), ("true", True), ("on", True),
                                          ("0", False), ("no", False), ("", False)])
def test_flag_parsing(val, expected):
    assert shi.is_self_heal_enabled({"AIFACTORY_SELF_HEAL": val}) is expected


# ---- item 1: self_heal_subtask ------------------------------------------

async def test_self_heal_subtask_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFACTORY_SELF_HEAL", raising=False)
    called = []
    out = await shi.self_heal_subtask(
        label="st1", attempt=lambda n: called.append(n), project_dir=tmp_path)
    assert out is None and called == []  # never ran the attempt


async def test_self_heal_subtask_runs_and_passes(monkeypatch, tmp_path):
    _enable(monkeypatch)
    calls = []

    async def attempt(n):
        calls.append(n)

    class _Pass:
        passed = True
        failures: list = []

    async def verify():
        return _Pass()

    out = await shi.self_heal_subtask(
        label="st1", attempt=attempt, project_dir=tmp_path, verify=verify)
    assert out is not None and out.ok and calls == [1]


# ---- item 2: review tier --------------------------------------------------

def test_assess_review_tier_flags_high_risk_paths():
    plan = {"phases": [{"subtasks": [
        {"id": "s1", "files_to_modify": ["app/auth/login.py"]}]}]}
    a = shi.assess_review_tier(plan)
    assert a is not None and a.pre_merge_gate is True


def test_assess_review_tier_trusted_low_risk():
    plan = {"phases": [{"subtasks": [{"id": "s1", "files_to_create": ["app/util.py"]}]}]}
    a = shi.assess_review_tier(plan, trusted=True)
    assert a is not None and a.pre_merge_gate is False


# ---- item 3: security pre-merge gate -------------------------------------

_SECRET_DIFF = (
    "diff --git a/config.py b/config.py\n"
    "+++ b/config.py\n"
    "@@ -0,0 +1 @@\n"
    '+AKIAIOSFODNN7EXAMPLE = "x"\n'
)


async def test_security_gate_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("AIFACTORY_SELF_HEAL", raising=False)
    assert await shi.security_pre_merge_gate(_SECRET_DIFF) is None


async def test_security_gate_blocks_on_secret(monkeypatch, tmp_path):
    _enable(monkeypatch)
    decision = await shi.security_pre_merge_gate(_SECRET_DIFF, project_dir=tmp_path)
    assert decision is not None and decision.blocked is True


async def test_security_gate_passes_clean_diff(monkeypatch, tmp_path):
    _enable(monkeypatch)
    clean = ("diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+def add(a, b):\n")
    decision = await shi.security_pre_merge_gate(clean, project_dir=tmp_path)
    assert decision is not None and decision.blocked is False


async def test_security_gate_empty_diff_is_noop(monkeypatch):
    _enable(monkeypatch)
    assert await shi.security_pre_merge_gate("") is None


# ---- item 4: artifacts ---------------------------------------------------

def test_emit_plan_artifact_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFACTORY_SELF_HEAL", raising=False)
    assert shi.emit_plan_artifact(tmp_path, {"feature": "x", "phases": []}) is None


def test_emit_plan_artifact_writes(monkeypatch, tmp_path):
    _enable(monkeypatch)
    plan = {"feature": "gw", "phases": [{"phase": 1, "name": "M",
            "subtasks": [{"id": "s1", "description": "do"}]}]}
    art = shi.emit_plan_artifact(tmp_path, plan)
    assert art is not None
    assert (tmp_path / "artifacts" / "plan.md").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
