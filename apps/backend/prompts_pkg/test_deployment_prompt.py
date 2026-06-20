"""Tests for the RFC-0013 deployment prompt injection (get_deployment_context, #643)."""

from __future__ import annotations

import json
from pathlib import Path

from prompts_pkg.prompts import get_deployment_context


def _write_contract(
    spec_dir: Path, contract: dict, *, where: str = "context/task_contract.json"
) -> None:
    path = spec_dir / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract))


def _deployment(**overrides: object) -> dict:
    block = {
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


def test_renders_scans_gates_and_verification(tmp_path: Path):
    _write_contract(tmp_path, _deployment())
    block = get_deployment_context(tmp_path)
    assert "## DEPLOYMENT CONTEXT" in block
    assert "secret-scan, sast, dependency-audit" in block
    assert "ci-green" in block
    assert "github-actions" in block
    assert "helm" in block
    assert "VAL-2 / dry-run" in block


def test_absent_contract_returns_empty(tmp_path: Path):
    assert get_deployment_context(tmp_path) == ""


def test_absent_deployment_block_returns_empty(tmp_path: Path):
    _write_contract(tmp_path, {"contract_version": "2"})
    assert get_deployment_context(tmp_path) == ""


def test_empty_deployment_block_returns_empty(tmp_path: Path):
    _write_contract(tmp_path, {"deployment": {}})
    assert get_deployment_context(tmp_path) == ""


def test_needs_pipeline_suggests_golden_path_template(tmp_path: Path):
    _write_contract(tmp_path, _deployment(needs_pipeline=True, ci_system="github-actions"))
    block = get_deployment_context(tmp_path)
    assert "needs_pipeline=true" in block
    assert "scaffolder_execute-template" in block
    assert "template:default/ci-cd-github-actions" in block


def test_needs_pipeline_gitlab_template(tmp_path: Path):
    _write_contract(tmp_path, _deployment(needs_pipeline=True, ci_system="gitlab-ci"))
    block = get_deployment_context(tmp_path)
    assert "template:default/ci-cd-gitlab-ci" in block


def test_production_carries_never_autonomous_warning(tmp_path: Path):
    _write_contract(
        tmp_path,
        _deployment(production_classification="production", risk_class="high"),
    )
    block = get_deployment_context(tmp_path)
    assert "VAL-4" in block
    assert "NEVER deployed autonomously" in block


def test_falls_back_to_implementation_plan(tmp_path: Path):
    _write_contract(tmp_path, _deployment(), where="implementation_plan.json")
    assert "secret-scan" in get_deployment_context(tmp_path)


def test_malformed_json_never_raises(tmp_path: Path):
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "task_contract.json").write_text("{not json")
    assert get_deployment_context(tmp_path) == ""


def test_partial_block_renders_only_present_fields(tmp_path: Path):
    _write_contract(tmp_path, {"deployment": {"risk_class": "low"}})
    block = get_deployment_context(tmp_path)
    assert "## DEPLOYMENT CONTEXT" in block
    assert "low" in block
    # No scans declared => no required-scans line.
    assert "Required scans" not in block
