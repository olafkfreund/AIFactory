"""Tests for the RFC-0013 deployment prompt injection (get_deployment_context, #643)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from core import contract_trust
from prompts_pkg.prompts import get_deployment_context


def _write_contract(
    spec_dir: Path,
    contract: dict[str, object],
    *,
    where: str = "context/task_contract.json",
) -> None:
    path = spec_dir / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract))


def _deployment(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "ci_system": "github-actions",
        "deploy_system": "helm",
        "risk_class": "medium",
        "deploy_target_environments": ["dev", "staging"],
        "production_classification": "preprod",
        "required_scans": ["secret-scan", "sast", "dependency-audit"],
        "system_gates": ["ci-green"],
        "deploy_verification": {"target_level": "VAL-2", "mode": "dry-run"},
    }
    block.update(overrides)
    return {"deployment": block}


def test_renders_scans_gates_and_verification(tmp_path: Path) -> None:
    _write_contract(tmp_path, _deployment())
    block = get_deployment_context(tmp_path)
    assert "## DEPLOYMENT CONTEXT" in block
    assert "secret-scan, sast, dependency-audit" in block
    assert "ci-green" in block
    assert "github-actions" in block
    assert "helm" in block
    assert "VAL-2 / dry-run" in block


def test_absent_contract_returns_empty(tmp_path: Path) -> None:
    assert get_deployment_context(tmp_path) == ""


def test_absent_deployment_block_returns_empty(tmp_path: Path) -> None:
    _write_contract(tmp_path, {"contract_version": "2"})
    assert get_deployment_context(tmp_path) == ""


def test_empty_deployment_block_returns_empty(tmp_path: Path) -> None:
    _write_contract(tmp_path, {"deployment": {}})
    assert get_deployment_context(tmp_path) == ""


def test_needs_pipeline_suggests_golden_path_template(tmp_path: Path) -> None:
    _write_contract(
        tmp_path, _deployment(needs_pipeline=True, ci_system="github-actions")
    )
    block = get_deployment_context(tmp_path)
    assert "needs_pipeline=true" in block
    assert "scaffolder_execute-template" in block
    assert "template:default/ci-cd-github-actions" in block


def test_needs_pipeline_gitlab_template(tmp_path: Path) -> None:
    _write_contract(tmp_path, _deployment(needs_pipeline=True, ci_system="gitlab-ci"))
    block = get_deployment_context(tmp_path)
    assert "template:default/ci-cd-gitlab-ci" in block


def test_production_carries_never_autonomous_warning(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        _deployment(production_classification="production", risk_class="high"),
    )
    block = get_deployment_context(tmp_path)
    assert "VAL-4" in block
    assert "NEVER deployed autonomously" in block


def test_falls_back_to_implementation_plan(tmp_path: Path) -> None:
    _write_contract(tmp_path, _deployment(), where="implementation_plan.json")
    assert "secret-scan" in get_deployment_context(tmp_path)


def test_malformed_json_never_raises(tmp_path: Path) -> None:
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "task_contract.json").write_text("{not json")
    assert get_deployment_context(tmp_path) == ""


def test_partial_block_renders_only_present_fields(tmp_path: Path) -> None:
    _write_contract(tmp_path, {"deployment": {"risk_class": "low"}})
    block = get_deployment_context(tmp_path)
    assert "## DEPLOYMENT CONTEXT" in block
    assert "low" in block
    # No scans declared => no required-scans line.
    assert "Required scans" not in block


@pytest.fixture(autouse=True)
def _no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(contract_trust.ENV, raising=False)


def test_hold_withholds_block_and_logs_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_contract(tmp_path, _deployment())
    monkeypatch.setenv(contract_trust.ENV, "hold")
    with caplog.at_level(logging.WARNING):
        assert get_deployment_context(tmp_path) == ""
    assert "held by server" in caplog.text


def test_digest_mismatch_withholds_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path, _deployment())
    monkeypatch.setenv(contract_trust.ENV, "0" * 64)
    assert get_deployment_context(tmp_path) == ""


def test_verified_renders_same_block_as_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_contract(tmp_path, _deployment())
    legacy = get_deployment_context(tmp_path)
    assert legacy
    monkeypatch.setenv(
        contract_trust.ENV, contract_trust.contract_digest(_deployment())
    )
    assert get_deployment_context(tmp_path) == legacy


def test_hold_ignores_plan_fallback_hole_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_contract(tmp_path, _deployment(), where="implementation_plan.json")
    monkeypatch.setenv(contract_trust.ENV, "hold")
    with caplog.at_level(logging.WARNING):
        assert get_deployment_context(tmp_path) == ""
    assert "implementation_plan.json is not verified" in caplog.text


def test_plan_approval_trace_ignores_fallback_hole_b(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        {**_deployment(), "approval": {"by": "pfactory"}},
        where="implementation_plan.json",
    )
    assert get_deployment_context(tmp_path) == ""


def test_non_dict_plan_fallback_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "implementation_plan.json").write_text("[1, 2]")
    assert get_deployment_context(tmp_path) == ""


def test_malformed_plan_fallback_never_raises(tmp_path: Path) -> None:
    (tmp_path / "implementation_plan.json").write_text("{not json")
    assert get_deployment_context(tmp_path) == ""
