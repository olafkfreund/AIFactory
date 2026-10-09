"""The merge gate acts only on the contract PFactory signed (#1667).

The coding agent has Write/Edit on ``{spec_path}/**``, so ``context/task_contract.json``
is agent-controlled. The signed contract is stored at ingest in the web server's
database (``TrustedRecord``); ``merge_disposition`` must decide from THAT record and
hold on any divergence. Test numbers follow the spec.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_WS = Path(__file__).resolve().parents[1]
_BACKEND = _WS.parents[0] / "backend"
for _p in (str(_WS), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli import workspace_commands  # noqa: E402
from pfactory import tfactory_client as tc  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402
from server.services.trusted_contract import host_isolated  # noqa: E402
from server.services.trusted_contract_store import (  # noqa: E402
    LOOKUP_FAILED,
    TrustedRecord,
    spec_key_for_dir,
)
from trusted_plan import APPROVAL_KEY, ingest_trusted_plan, sign_plan  # noqa: E402

KEY = "test-pfactory-key-1667"
HOLD = pe.HOLD_BLOCKING_DISPOSITION
TS = "2026-10-08T10:00:00Z"


@pytest.fixture(autouse=True)
def _isolated_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to an isolated host (D4-i); tests override to leave it."""
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    # kubejob counts as isolated only with the durable store (#1667).
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: True)
    monkeypatch.delenv("AIFACTORY_AGENT_SANDBOX", raising=False)
    monkeypatch.delenv("AIFACTORY_AGENT_SANDBOX_PIDNS", raising=False)
    monkeypatch.delenv("AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS", raising=False)
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY__T1", KEY)


def _plan(deployment: dict[str, Any] | None = None) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "feature": "Contract tamper gate",
        "workflow_type": "feature",
        "phases": [
            {
                "id": "p1",
                "name": "Endpoint",
                "parallel_safe": True,
                "subtasks": [
                    {
                        "id": "st1",
                        "description": "status endpoint",
                        "status": "pending",
                        "files_to_create": ["app/status.py"],
                    }
                ],
            }
        ],
        "tfactory": {"lanes": ["functional"]},
    }
    if deployment is not None:
        plan["deployment"] = deployment
    return plan


def _sign(plan: dict[str, Any]) -> dict[str, Any]:
    plan = copy.deepcopy(plan)
    plan.pop(APPROVAL_KEY, None)
    plan[APPROVAL_KEY] = sign_plan(
        plan, key=KEY, approved_by="pfactory", approval_timestamp=TS, kid="t1"
    )
    return plan


def _spec(
    tmp_path: Path, deployment: dict[str, Any] | None = None, *, tier: str = "low"
) -> tuple[Path, dict[str, Any]]:
    """A spec dir written the way ``ingest_trusted_plan`` writes it."""
    signed = _sign(_plan(deployment))
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    result = ingest_trusted_plan(spec, signed)
    assert result.ok, result.reasons
    meta_file = spec / "task_metadata.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    meta["reviewTier"] = tier
    meta_file.write_text(json.dumps(meta))
    return spec, signed


def _contract_file(spec: Path) -> Path:
    return spec / "context" / "task_contract.json"


def _rec(contract: dict[str, Any], isolation: str | None = "kubejob") -> TrustedRecord:
    return TrustedRecord(contract=contract, build_isolation=isolation)


PROD = {"production_classification": "production"}


def test_1_stripped_deployment_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, PROD)
    on_disk = json.loads(_contract_file(spec).read_text())
    on_disk.pop("deployment")
    _contract_file(spec).write_text(json.dumps(on_disk))

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


def test_2_deleted_contract_and_traces_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, PROD)
    _contract_file(spec).unlink()
    req = json.loads((spec / "requirements.json").read_text())
    req.get("provenance", {}).pop("trusted_plan", None)
    (spec / "requirements.json").write_text(json.dumps(req))
    plan = json.loads((spec / "implementation_plan.json").read_text())
    plan.pop(APPROVAL_KEY, None)
    plan.pop("deployment", None)
    (spec / "implementation_plan.json").write_text(json.dumps(plan))

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


def test_3_edited_and_resigned_contract_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, PROD)
    edited = json.loads(_contract_file(spec).read_text())
    edited.pop("deployment")
    _contract_file(spec).write_text(json.dumps(_sign(edited)))

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


def test_4_satisfied_gates_in_metadata_ignored(tmp_path: Path) -> None:
    gated = {"system_gates": ["human-approval"]}
    spec, signed = _spec(tmp_path, gated)
    meta = json.loads((spec / "task_metadata.json").read_text())
    meta["satisfiedSystemGates"] = ["human-approval"]
    meta["satisfied_system_gates"] = ["human-approval"]
    (spec / "task_metadata.json").write_text(json.dumps(meta))

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


def test_5_retired_kid_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec, signed = _spec(tmp_path)
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS", "pfactory/t1")

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


def test_6_record_contract_altered_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path)
    altered = copy.deepcopy(signed)
    altered["feature"] = "Contract tamper gatf"  # one character

    assert pe.merge_disposition(spec, "low", trusted=_rec(altered)) == HOLD


def test_9_lookup_failed_holds(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)

    assert pe.merge_disposition(spec, "low", trusted=LOOKUP_FAILED) == HOLD


def test_10_stamp_none_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path)

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed, "none")) == HOLD


def test_11_empty_stamp_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path)

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed, None)) == HOLD


def test_14_record_kubejob_but_host_not_isolated_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path)
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    monkeypatch.delenv("AIFACTORY_AGENT_SANDBOX", raising=False)

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


def test_15_legacy_task_on_non_isolated_host_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    monkeypatch.delenv("AIFACTORY_AGENT_SANDBOX", raising=False)

    assert pe.merge_disposition(spec, "low", trusted=None) == HOLD


def test_16_untouched_record_low_tier_auto_merges(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path)

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == "auto-merge"


def test_17a_legacy_without_contract_unchanged(tmp_path: Path) -> None:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)

    assert pe.merge_disposition(spec, "low", trusted=None) == "auto-merge"


def test_17b_legacy_unparseable_contract_holds(tmp_path: Path) -> None:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    (spec / "context").mkdir(parents=True)
    _contract_file(spec).write_text("{not json")

    assert pe.merge_disposition(spec, "low", trusted=None) == HOLD


def test_18_in_flight_trusted_task_without_record_holds(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)
    req = json.loads((spec / "requirements.json").read_text())
    assert req["provenance"]["trusted_plan"] is True

    assert pe.merge_disposition(spec, "low", trusted=None) == HOLD


def test_19_path_floor_blocks_on_stamp_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path, tier="auto")
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / "001-x").mkdir(parents=True)
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    monkeypatch.setattr(
        workspace_commands, "_get_changed_files_from_git", lambda *_a, **_k: []
    )

    tier, floor = pe.apply_path_risk_floor(
        tmp_path, spec, "001-x", "dev", "auto", trusted=_rec(signed, "none")
    )

    assert floor == "blocking"
    assert tier == "blocking"


def test_20_handoff_uses_the_record_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path, PROD)
    on_disk = json.loads(_contract_file(spec).read_text())
    on_disk.pop("deployment")
    on_disk.pop("tfactory")
    _contract_file(spec).write_text(json.dumps(on_disk))
    (spec / "task_metadata.json").write_text(
        json.dumps({"phaseModels": {"qa": "ollama:x"}})
    )
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: (None, None))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "tfactory")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)

    payload = tc.build_ingest_payload(spec, "001-x", contract=signed)

    assert payload["contract"]["deployment"] == PROD
    assert payload["contract"]["tfactory"] == signed["tfactory"]
    assert payload["contract"]["execution"]["phase_models"]["qa"] == "ollama:x"

    held = tc.build_ingest_payload(spec, "001-x", contract={})
    assert not held.get("contract")


def test_kubejob_without_the_durable_store_is_not_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kubejob without DATABASE_URL falls back to in-pod builds (#1667 review)."""
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: False)
    assert host_isolated() is False


def test_spec_key_ignores_a_symlinked_spec_dir(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    spec = tmp_path / "specs" / "001-x"
    spec.parent.mkdir()
    before = spec_key_for_dir(spec)
    spec.symlink_to(real)
    assert spec_key_for_dir(spec) == before


def test_pid_namespaced_sandbox_host_is_not_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4 narrowed: run.py in the sandbox carries the server env (#1680)."""
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "strict")
    monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX_PIDNS", "1")
    assert host_isolated() is False
