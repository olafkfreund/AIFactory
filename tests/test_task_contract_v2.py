#!/usr/bin/env python3
"""
Task Contract v2 ingest — execution profile + contract_version 2 (epic #424)
============================================================================

AIFactory must accept a signed contract_version "2" plan (RFC-0002) carrying an
`execution` block and apply it to task_metadata.json so the executor honors
model/parallel/workers/complexity/skills and skips planning — without re-running
the planner. v1 plans (no execution block) must keep working unchanged.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from trusted_plan import (  # noqa: E402
    APPROVAL_KEY,
    SUPPORTED_CONTRACT_VERSIONS,
    apply_execution_profile,
    execution_profile_to_metadata,
    ingest_trusted_plan,
    sign_plan,
    verify_plan_signature,
)

KEY = "k-pfactory"
KEYRING = {"pfactory": KEY}
TS = "2026-06-06T12:00:00Z"


def _plan(execution: dict | None = None, tfactory: dict | None = None) -> dict:
    p = {
        "contract_version": "2",
        "feature": "rate limiting",
        "workflow_type": "feature",
        "phases": [{
            "id": "p1", "name": "Modules", "parallel_safe": True,
            "subtasks": [
                {"id": "st1", "description": "middleware", "status": "pending",
                 "files_to_create": ["app/middleware/ratelimit.py"]},
            ],
        }],
    }
    if execution is not None:
        p["execution"] = execution
    if tfactory is not None:
        p["tfactory"] = tfactory
    return p


def _signed(plan: dict) -> dict:
    plan = json.loads(json.dumps(plan))
    plan[APPROVAL_KEY] = sign_plan(plan, key=KEY, approved_by="pfactory", approval_timestamp=TS)
    return plan


# ---- contract_version 2 (#425) -------------------------------------------

def test_version_2_is_supported():
    assert "2" in SUPPORTED_CONTRACT_VERSIONS and "1" in SUPPORTED_CONTRACT_VERSIONS


def test_signed_v2_plan_verifies_and_signature_covers_execution():
    plan = _signed(_plan(execution={"parallel": True, "workers": 4}))
    ok, reason = verify_plan_signature(plan, keyring=KEYRING)
    assert ok, reason
    # Tampering with the execution block must break the signature (it is signed).
    plan["execution"]["workers"] = 99
    ok2, _ = verify_plan_signature(plan, keyring=KEYRING)
    assert ok2 is False


# ---- execution profile mapping (#426) ------------------------------------

def test_execution_profile_to_metadata_maps_keys():
    meta = execution_profile_to_metadata({
        "model": "claude-sonnet-4-6",
        "phase_models": {"coding": "sonnet"},
        "phase_thinking": {"qa": "high"},
        "parallel": True, "workers": 4, "complexity": "standard",
        "review_tier": "async",
        "skills": [{"id": "python/fastapi"}],
        "skip_planning": True,
        "provider": "claude",  # unmapped — ignored
    })
    assert meta["model"] == "claude-sonnet-4-6"
    assert meta["phaseModels"] == {"coding": "sonnet"}
    assert meta["phaseThinking"] == {"qa": "high"}
    assert meta["parallel"] is True and meta["workers"] == 4
    assert meta["complexity"] == "standard"
    assert meta["reviewTier"] == "async"
    assert meta["selectedSkills"] == [{"id": "python/fastapi"}]
    assert meta["skipPlanning"] is True
    assert meta["isAutoProfile"] is True  # phaseModels present
    assert "provider" not in meta


def test_execution_profile_maps_budget_usd(tmp_path):
    """#45 P2: execution.budget_usd → task_metadata.budgetUsd (observe-only)."""
    meta = execution_profile_to_metadata({"budget_usd": 5.0})
    assert meta["budgetUsd"] == 5.0
    # Absent budget → no budgetUsd key (back-compat).
    assert "budgetUsd" not in execution_profile_to_metadata({"model": "x"})
    # Applied to the spec's task_metadata.json so completion.py can read it.
    written = apply_execution_profile(tmp_path, _plan(execution={"budget_usd": 2.5}))
    assert written["budgetUsd"] == 2.5
    on_disk = json.loads((tmp_path / "task_metadata.json").read_text())
    assert on_disk["budgetUsd"] == 2.5


def test_apply_execution_profile_writes_and_merges(tmp_path):
    (tmp_path / "task_metadata.json").write_text(json.dumps({"baseBranch": "dev"}))
    written = apply_execution_profile(tmp_path, _plan(execution={"parallel": True, "workers": 3}))
    assert written == {"parallel": True, "workers": 3}
    meta = json.loads((tmp_path / "task_metadata.json").read_text())
    assert meta["parallel"] is True and meta["workers"] == 3
    assert meta["baseBranch"] == "dev"  # preserved


def test_apply_execution_profile_noop_without_block(tmp_path):
    assert apply_execution_profile(tmp_path, _plan()) == {}
    assert not (tmp_path / "task_metadata.json").exists()


# ---- end-to-end ingest (#425/#426/#427) ----------------------------------

def test_ingest_v2_installs_plan_and_task_metadata(tmp_path):
    spec_dir = tmp_path / "spec"
    plan = _signed(_plan(
        execution={"model": "claude-sonnet-4-6", "parallel": True, "workers": 4,
                   "complexity": "standard", "skip_planning": True},
        tfactory={"lanes": ["unit"]},
    ))
    result = ingest_trusted_plan(spec_dir, plan, keyring=KEYRING)
    assert result.ok, result.reasons
    # plan installed verbatim (tfactory block preserved for the TFactory handover)
    installed = json.loads((spec_dir / "implementation_plan.json").read_text())
    assert installed["tfactory"] == {"lanes": ["unit"]}
    # execution profile applied to task_metadata.json (executor honors these)
    meta = json.loads((spec_dir / "task_metadata.json").read_text())
    assert meta["model"] == "claude-sonnet-4-6"
    assert meta["parallel"] is True and meta["workers"] == 4
    assert meta["complexity"] == "standard" and meta["skipPlanning"] is True


def test_ingest_v1_plan_still_works_no_metadata(tmp_path):
    spec_dir = tmp_path / "spec"
    plan = _plan()                       # no execution block
    plan["contract_version"] = "1"
    plan = _signed(plan)
    result = ingest_trusted_plan(spec_dir, plan, keyring=KEYRING)
    assert result.ok, result.reasons
    assert (spec_dir / "implementation_plan.json").exists()
    assert not (spec_dir / "task_metadata.json").exists()  # nothing to apply


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
