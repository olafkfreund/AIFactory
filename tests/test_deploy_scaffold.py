"""Tests for the deterministic deploy scaffolder (RFC-0013 consuming side)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents.deploy_scaffold import scaffold_deploy  # noqa: E402


def test_no_block_writes_nothing(tmp_path: Path) -> None:
    assert scaffold_deploy(None, tmp_path) == []
    assert scaffold_deploy({"deploy_system": "helm"}, tmp_path) == []
    assert scaffold_deploy({"deploy_system": "none"}, tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_gcp_scaffold_writes_proven_artifacts(tmp_path: Path) -> None:
    written = scaffold_deploy(
        {"deploy_system": "gcp-cloud-run", "managed_services": ["postgres", "redis"]},
        tmp_path,
    )
    assert written == [
        ".github/workflows/deploy.yml",
        ".tfactory.yml",
        "infra/main.tf",
    ]
    tf = (tmp_path / "infra" / "main.tf").read_text()
    assert "google_cloud_run_v2_service" in tf
    assert "google_redis_instance" in tf


def test_azure_scaffold_uses_container_apps(tmp_path: Path) -> None:
    scaffold_deploy({"deploy_system": "azure-container-apps"}, tmp_path)
    tf = (tmp_path / "infra" / "main.tf").read_text()
    assert "azurerm_container_app" in tf


def test_aws_scaffold(tmp_path: Path) -> None:
    scaffold_deploy({"deploy_system": "aws-app-runner"}, tmp_path)
    assert "aws_apprunner_service" in (tmp_path / "infra" / "main.tf").read_text()


def test_does_not_clobber_existing_coder_files(tmp_path: Path) -> None:
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text("# the coder's own infra\n")
    written = scaffold_deploy({"deploy_system": "gcp-cloud-run"}, tmp_path)
    assert "infra/main.tf" not in written
    assert (tmp_path / "infra" / "main.tf").read_text() == "# the coder's own infra\n"


def test_scaffold_for_spec_reads_contract(tmp_path: Path) -> None:
    from agents.deploy_scaffold import scaffold_deploy_for_spec

    spec = tmp_path / ".aifactory" / "specs" / "001-game"
    (spec / "context").mkdir(parents=True)
    (spec / "context" / "task_contract.json").write_text(
        '{"deployment": {"deploy_system": "gcp-cloud-run"}}'
    )
    written = scaffold_deploy_for_spec(spec)
    assert "infra/main.tf" in written
    # written into the worktree root (parent of .aifactory), not the spec dir
    assert (tmp_path / "infra" / "main.tf").exists()


def test_scaffold_for_spec_no_contract(tmp_path: Path) -> None:
    from agents.deploy_scaffold import scaffold_deploy_for_spec

    assert scaffold_deploy_for_spec(tmp_path / "nope") == []


# ── #1673: scaffold only on a contract the server verified ──────────────

import json  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402
from agents import deploy_scaffold  # noqa: E402
from core.contract_trust import ENV, contract_digest  # noqa: E402

_GCP = {"deployment": {"deploy_system": "gcp-cloud-run"}}


@pytest.fixture(autouse=True)
def _no_trusted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


def _spec(tmp_path: Path, obj: dict) -> Path:
    spec = tmp_path / ".aifactory" / "specs" / "001-game"
    (spec / "context").mkdir(parents=True)
    (spec / "context" / "task_contract.json").write_text(json.dumps(obj))
    return spec


def test_scaffold_for_spec_verified_digest_scaffolds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path, _GCP)
    monkeypatch.setenv(ENV, contract_digest(_GCP))
    written = deploy_scaffold.scaffold_deploy_for_spec(spec)
    assert "infra/main.tf" in written
    assert (tmp_path / "infra" / "main.tf").exists()


@pytest.mark.parametrize("case", ["hold", "other-digest", "trace"])
def test_scaffold_for_spec_withheld(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    contract = {**_GCP, "approval": {"sig": "x"}} if case == "trace" else _GCP
    spec = _spec(tmp_path, contract)
    if case == "hold":
        monkeypatch.setenv(ENV, "hold")
    elif case == "other-digest":
        monkeypatch.setenv(ENV, contract_digest({"feature": "other"}))
    with caplog.at_level(logging.WARNING, logger="agents.deploy_scaffold"):
        assert deploy_scaffold.scaffold_deploy_for_spec(spec) == []
    assert not (tmp_path / "infra").exists()
    assert "[trusted-contract] deploy scaffold withheld" in caplog.text


def test_scaffold_for_spec_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_spec: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(deploy_scaffold, "trusted_contract", boom, raising=False)
    assert deploy_scaffold.scaffold_deploy_for_spec(_spec(tmp_path, _GCP)) == []
