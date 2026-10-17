"""The merge gate acts only on the contract PFactory signed (#1667).

The coding agent has Write/Edit on ``{spec_path}/**``, so ``context/task_contract.json``
is agent-controlled. The signed contract is stored at ingest in the web server's
database (``TrustedRecord``); ``merge_disposition`` must decide from THAT record and
hold on any divergence. Test numbers follow the spec.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
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
from server.routes import execution as ex  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402
from server.services.trusted_contract import (  # noqa: E402
    handoff_contract,
    host_isolated,
)
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


def _plan(
    deployment: dict[str, Any] | None = None, review_tier: object = "auto"
) -> dict[str, Any]:
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
    if review_tier is not None:
        plan["execution"] = {"review_tier": review_tier}
    return plan


def _sign(plan: dict[str, Any]) -> dict[str, Any]:
    plan = copy.deepcopy(plan)
    plan.pop(APPROVAL_KEY, None)
    plan[APPROVAL_KEY] = sign_plan(
        plan, key=KEY, approved_by="pfactory", approval_timestamp=TS, kid="t1"
    )
    return plan


def _spec(
    tmp_path: Path,
    deployment: dict[str, Any] | None = None,
    *,
    tier: str = "low",
    review_tier: object = "auto",
) -> tuple[Path, dict[str, Any]]:
    """A spec dir written the way ``ingest_trusted_plan`` writes it."""
    signed = _sign(_plan(deployment, review_tier=review_tier))
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

    out = pe.merge_disposition(
        spec, "low", trusted=_rec(signed), fetch_signals=lambda: _FETCH_PASS
    )
    assert out == "auto-merge"


def test_17a_legacy_without_contract_merges_only_on_a_fetched_pass(
    tmp_path: Path,
) -> None:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)

    # #1697: nothing measured is not a pass.
    assert pe.merge_disposition(spec, "low", trusted=None) != "auto-merge"
    out = pe.merge_disposition(
        spec, "low", trusted=None, fetch_signals=lambda: _FETCH_PASS
    )
    assert out == "auto-merge"


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


async def _start_tfactory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rec_for: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Start a TFactory-routed trusted spec whose plan file was forged (#1750)."""
    spec, signed = _spec(tmp_path)
    req_file = spec / "requirements.json"
    req = json.loads(req_file.read_text()) if req_file.exists() else {}
    req["githubIssue"] = {
        "number": 1,
        "labels": ["pfactory", "handoff:tfactory", "type:testing"],
    }
    req_file.write_text(json.dumps(req))
    plan_file = spec / "implementation_plan.json"
    plan = json.loads(plan_file.read_text())
    plan["tfactory"] = {"lanes": ["forged"]}
    plan_file.write_text(json.dumps(plan))

    record = rec_for(signed)

    async def fake_lookup(_spec_dir: Path) -> TrustedRecord:
        return record

    sent: dict[str, Any] = {}

    async def fake_poster(
        _url: str, payload: dict[str, Any], _headers: Any
    ) -> dict[str, Any]:
        sent["payload"] = payload
        return {"status": 202, "ok": True, "body": "queued"}

    monkeypatch.setattr(ex, "load_projects", lambda: {"p": {"path": str(tmp_path)}})
    monkeypatch.setattr(ex, "lookup", fake_lookup)
    monkeypatch.setenv("TFACTORY_BASE_URL", "https://tf.example")
    monkeypatch.setattr(tc, "_httpx_poster", fake_poster)

    result = await ex.start_task("p:001-x", ex.StartTaskRequest(), None)
    return result, sent["payload"], signed


async def test_30_start_task_hands_off_the_signed_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, payload, signed = await _start_tfactory(tmp_path, monkeypatch, _rec)

    assert result["routed_to"] == "tfactory"
    assert payload["tfactory"] == signed["tfactory"]
    assert payload["tfactory"] != {"lanes": ["forged"]}


async def test_31_start_task_non_isolated_host_sends_empty_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")

    result, payload, _ = await _start_tfactory(tmp_path, monkeypatch, _rec)

    assert result["routed_to"] == "tfactory"
    assert payload["tfactory"] == {}


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


def test_a_record_never_verifies_on_a_non_isolated_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handoff and path floor get no record contract there, not only the merge."""
    spec, signed = _spec(tmp_path, tier="auto")
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / "001-x").mkdir(parents=True)
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    monkeypatch.setattr(
        workspace_commands, "_get_changed_files_from_git", lambda *_a, **_k: []
    )
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: False)
    assert handoff_contract(spec, _rec(signed)) == {}
    tier, floor = pe.apply_path_risk_floor(
        tmp_path, spec, "001-x", "dev", "auto", trusted=_rec(signed)
    )
    assert (tier, floor) == ("blocking", "blocking")


def _warn(caplog: pytest.LogCaptureFixture, level: int = logging.WARNING) -> None:
    caplog.set_level(level, logger=pe.logger.name)


def test_21_forged_low_below_signed_blocking_holds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _warn(caplog)
    spec, signed = _spec(tmp_path, review_tier="blocking")

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD
    assert "below signed" in caplog.text


def test_22_human_approval_cannot_clear_signed_blocking(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, review_tier="blocking")

    assert pe._clears_with_human_approval(spec, "auto", _rec(signed)) is False


def test_23_forged_low_below_signed_async_holds_async(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, review_tier="async")

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == "hold-async"


def test_24_tier_above_signed_is_kept_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _warn(caplog)
    spec, signed = _spec(tmp_path, review_tier="auto")

    assert pe.merge_disposition(spec, "blocking", trusted=_rec(signed)) == HOLD
    assert "below signed" not in caplog.text


def test_25_verified_contract_without_signed_tier_holds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _warn(caplog)
    spec, signed = _spec(tmp_path, review_tier=None)

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD
    assert "no signed review_tier" in caplog.text


@pytest.mark.parametrize("signed_tier", ["bogus", 1])
def test_26_unusable_signed_tier_holds(tmp_path: Path, signed_tier: object) -> None:
    spec, signed = _spec(tmp_path, review_tier=signed_tier)

    assert pe.merge_disposition(spec, "low", trusted=_rec(signed)) == HOLD


@pytest.mark.parametrize("tier", [None, ""])
def test_27_blank_tier_takes_signed_auto(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, tier: str | None
) -> None:
    _warn(caplog)
    spec, signed = _spec(tmp_path, review_tier="auto")

    out = pe.merge_disposition(
        spec, tier, trusted=_rec(signed), fetch_signals=lambda: _FETCH_PASS
    )
    assert out == "auto-merge"
    assert "below signed" not in caplog.text


def test_28_unrecognised_tier_not_raised_to_signed(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, review_tier="auto")

    assert pe.merge_disposition(spec, "bogus", trusted=_rec(signed)) == HOLD


def test_29_forged_tier_never_reaches_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _warn(caplog, logging.DEBUG)
    spec, signed = _spec(tmp_path, review_tier="auto")

    assert pe.merge_disposition(spec, "auto\nFORGED", trusted=_rec(signed)) == HOLD
    assert "FORGED" not in caplog.text


# ── #1673: the server verdict handed to the build process ───────────────

from core import contract_trust  # noqa: E402
from core.contract_trust import ENV, contract_digest, trusted_contract  # noqa: E402
from server.services import trusted_contract as tc_mod  # noqa: E402
from server.services.trusted_contract import spawn_env  # noqa: E402


@pytest.fixture(autouse=True)
def _no_trusted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


def _lookup(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    async def fake(_spec: Path) -> Any:
        return result

    monkeypatch.setattr("server.services.trusted_contract.lookup", fake)


async def test_spawn_env_verified_is_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path, PROD)
    _lookup(monkeypatch, _rec(signed))
    assert await spawn_env(spec) == {ENV: contract_digest(signed)}


@pytest.mark.parametrize(
    "case", ["lookup-failed", "none-stamp", "edited-disk", "trace", "subprocess"]
)
async def test_spawn_env_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    spec, signed = _spec(tmp_path, PROD)
    result: Any = _rec(signed)
    if case == "lookup-failed":
        result = LOOKUP_FAILED
    elif case == "none-stamp":
        result = _rec(signed, "none")
    elif case == "edited-disk":
        _contract_file(spec).write_text(json.dumps({**signed, "feature": "edited"}))
    elif case == "trace":
        result = None
    elif case == "subprocess":
        monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    _lookup(monkeypatch, result)
    assert await spawn_env(spec) == {ENV: "hold"}


async def test_spawn_env_legacy_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    _lookup(monkeypatch, None)
    assert await spawn_env(spec) == {ENV: "legacy"}


async def test_cross_side_digest_matches_pod_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path, PROD)
    _lookup(monkeypatch, _rec(signed))
    _contract_file(spec).write_text(
        json.dumps(dict(reversed(list(signed.items()))), indent=2)
    )
    env = await spawn_env(spec)
    monkeypatch.setenv(ENV, env[ENV])
    state, contract, _why = trusted_contract(spec)
    assert (state, contract) == ("verified", signed)


def test_has_trusted_trace_is_the_core_predicate() -> None:
    assert tc_mod.has_trusted_trace is contract_trust.has_trusted_trace


# --------------------------------------------------------------------------- #
# #1695: a verified task merges only on signals AIFactory fetched itself
# --------------------------------------------------------------------------- #

_FETCH_PASS = {"tfactory_verdict": "pass", "host_ci_green": True}


def test_1695_verified_green_fetch_auto_merges(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, tier="low")
    out = pe.merge_disposition(
        spec, "low", trusted=_rec(signed), fetch_signals=lambda: _FETCH_PASS
    )
    assert out == pe.AUTO_MERGE_DISPOSITION


@pytest.mark.parametrize(
    "fetched",
    [
        {"tfactory_verdict": None, "host_ci_green": True},
        {"tfactory_verdict": "pass", "host_ci_green": None},
    ],
)
def test_1695_verified_unknown_signal_does_not_merge(
    tmp_path: Path, fetched: dict[str, object]
) -> None:
    spec, signed = _spec(tmp_path, tier="low")
    out = pe.merge_disposition(
        spec, "low", trusted=_rec(signed), fetch_signals=lambda: fetched
    )
    assert out != pe.AUTO_MERGE_DISPOSITION


def _boom() -> dict[str, object]:
    raise RuntimeError("tfactory down")


def test_1695_verified_raising_fetch_holds(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, tier="low")
    out = pe.merge_disposition(spec, "low", trusted=_rec(signed), fetch_signals=_boom)
    assert out == pe.HOLD_BLOCKING_DISPOSITION


def _legacy_spec(tmp_path: Path) -> Path:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    return spec


def test_1697_legacy_calls_the_fetch(tmp_path: Path) -> None:
    spec = _legacy_spec(tmp_path)
    out = pe.merge_disposition(spec, "low", trusted=None, fetch_signals=_boom)
    assert out == pe.HOLD_BLOCKING_DISPOSITION


@pytest.mark.parametrize(
    "fetched",
    [
        {"tfactory_verdict": None, "host_ci_green": True},
        {"tfactory_verdict": "pass", "host_ci_green": None},
    ],
)
def test_1697_legacy_unknown_signal_does_not_merge(
    tmp_path: Path, fetched: dict[str, object]
) -> None:
    spec = _legacy_spec(tmp_path)
    out = pe.merge_disposition(spec, "low", trusted=None, fetch_signals=lambda: fetched)
    assert out != pe.AUTO_MERGE_DISPOSITION


def test_1697_legacy_path_floor_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(pe.PATH_RISK_FLOOR_ENV, raising=False)
    spec = _legacy_spec(tmp_path)
    out = pe.merge_disposition(
        spec, "low", trusted=None, fetch_signals=lambda: _FETCH_PASS, floor="blocking"
    )
    assert out == pe.HOLD_BLOCKING_DISPOSITION


def test_1697_legacy_unrecognised_tier_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review: an unrecognised tier ranks below auto, so the floor must not
    # "raise" it into a mergeable tier.
    monkeypatch.delenv(pe.PATH_RISK_FLOOR_ENV, raising=False)
    spec = _legacy_spec(tmp_path)
    out = pe.merge_disposition(
        spec, "blocker", trusted=None, fetch_signals=lambda: _FETCH_PASS, floor="auto"
    )
    assert out == pe.HOLD_BLOCKING_DISPOSITION


def test_1695_human_approval_cannot_clear_a_red_fetch(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, tier="low")
    red = lambda: {"tfactory_verdict": "fail", "host_ci_green": True}  # noqa: E731
    assert (
        pe._clears_with_human_approval(spec, "low", _rec(signed), fetch_signals=red)
        is False
    )


@pytest.mark.parametrize("verified", [True, False])
def test_1697_endgame_arms_the_gate_for_legacy_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified: bool
) -> None:
    spec, signed = _spec(tmp_path, tier="low")
    captured: dict[str, object] = {}

    async def _fake_watch(**kwargs: object) -> dict[str, object]:
        captured["gate"] = kwargs.get("merge_gate")
        return {"merged": False, "reason": "stub"}

    monkeypatch.setattr(pe, "watch_and_finish", _fake_watch)
    monkeypatch.setattr(pe, "create_pr", lambda **_k: 42)
    monkeypatch.setattr(pe, "request_copilot_review", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    if not verified:  # legacy: no contract, no trusted-plan provenance
        spec = _legacy_spec(tmp_path / "legacy")
    asyncio.run(
        pe.run_pr_endgame(
            spec_dir=spec,
            spec_id="001-x",
            worktree=spec,
            branch="aifactory/001-x",
            base="main",
            repo="owner/repo",
            auto_merge=True,
            review_tier="low",
            reviewer="copilot",
            background=False,
            trusted=_rec(signed) if verified else None,
        )
    )
    assert callable(captured["gate"])


# --- #1695 review: the gate built by _build_merge_gate is exercised ---------- #


def _gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str | None,
    ci: bool | None,
    human: bool = False,
) -> tuple[Any, list[str]]:
    spec, signed = _spec(tmp_path, tier="low")
    heads: list[str] = []

    def fake_ci(owner: str, repo: str, sha: str, **_k: object) -> bool | None:
        heads.append(f"{owner}/{repo}@{sha}")
        return ci

    monkeypatch.setattr(pe, "host_ci_green", fake_ci)
    monkeypatch.setattr(tc, "tfactory_verdict", lambda _d, _s, **_k: verdict)
    gate = pe._build_merge_gate(
        spec,
        "001-x",
        ("o", "r"),
        _rec(signed),
        "low",
        None,
        human,
        lambda *_a, **_k: None,
    )
    return gate, heads


@pytest.mark.parametrize("human", [False, True])
def test_1695_gate_green_passes_the_exact_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, human: bool
) -> None:
    gate, heads = _gate(tmp_path, monkeypatch, verdict="pass", ci=True, human=human)
    assert gate("deadbeef") is True
    assert heads == ["o/r@deadbeef"]


@pytest.mark.parametrize(
    ("verdict", "ci"), [("pass", None), ("pass", False), ("fail", True), (None, True)]
)
@pytest.mark.parametrize("human", [False, True])
def test_1695_gate_red_or_unknown_is_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str | None,
    ci: bool | None,
    human: bool,
) -> None:
    gate, _ = _gate(tmp_path, monkeypatch, verdict=verdict, ci=ci, human=human)
    assert gate("deadbeef") is False


def test_1695_gate_is_set_when_resolve_contract_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path, tier="low")

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("lookup")

    monkeypatch.setattr("server.services.trusted_contract.resolve_contract", boom)
    gate = pe._build_merge_gate(
        spec,
        "001-x",
        ("o", "r"),
        _rec(signed),
        "low",
        None,
        False,
        lambda *_a, **_k: None,
    )
    assert gate is not None


@pytest.mark.parametrize(("verdict", "merges"), [("pass", True), (None, False)])
def test_1697_legacy_gate_needs_a_real_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str | None, merges: bool
) -> None:
    spec = _legacy_spec(tmp_path)
    monkeypatch.setattr(pe, "host_ci_green", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "tfactory_verdict", lambda _d, _s, **_k: verdict)
    gate = pe._build_merge_gate(
        spec, "001-x", ("o", "r"), None, "low", None, False, lambda *_a, **_k: None
    )
    assert gate("deadbeef") is merges


# --- #1698: the PR body states the tier the gate decides on ---------------- #


@pytest.mark.parametrize(
    ("tier", "signed", "expected"),
    [
        ("low", "blocking", ("blocking", True)),
        ("blocking", "auto", ("blocking", False)),
        (None, "auto", ("auto", False)),
        ("auto", "auto", ("auto", False)),
    ],
)
def test_raise_to_signed(
    tier: str | None, signed: str, expected: tuple[str, bool]
) -> None:
    assert pe._raise_to_signed(tier, signed) == expected


def test_raise_to_signed_unrecognised_tier_is_preserved() -> None:
    effective, below = pe._raise_to_signed("bogus", "auto")
    assert effective == "bogus"
    assert below is True


def test_review_tier_note_flags_tier_below_signed(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, review_tier="blocking")

    note = pe.review_tier_note(spec, "low", trusted=_rec(signed))

    assert "Effective review tier: blocking (signed floor blocking)." in note
    assert "Possible tampering" in note
    assert "reviewTier low is below the signed tier blocking" in note
    assert "the merge gate decides on blocking." in note


def test_review_tier_note_matching_signed_has_no_tampering_warning(
    tmp_path: Path,
) -> None:
    spec, signed = _spec(tmp_path, review_tier="auto")

    note = pe.review_tier_note(spec, "auto", trusted=_rec(signed))

    assert note == "Effective review tier: auto (signed floor auto)."
    assert "Possible tampering" not in note


def test_review_tier_note_legacy_without_contract(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)
    _contract_file(spec).unlink()

    note = pe.review_tier_note(spec, "low", trusted=None)

    assert "Review tier: low (not checked" in note


def test_review_tier_note_crafted_tier_uses_constant_label(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)
    _contract_file(spec).unlink()

    note = pe.review_tier_note(spec, "low\n\n@evil", trusted=None)

    assert "unrecognised" in note
    assert "@evil" not in note


def test_1698_endgame_body_flags_tier_below_signed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, signed = _spec(tmp_path, review_tier="blocking")
    captured: dict[str, str] = {}

    async def _fake_watch(**_k: object) -> dict[str, object]:
        return {"merged": False, "reason": "stub"}

    def _fake_create_pr(**kwargs: Any) -> int:
        captured["body"] = kwargs["body"]
        return 42

    monkeypatch.setattr(pe, "watch_and_finish", _fake_watch)
    monkeypatch.setattr(pe, "create_pr", _fake_create_pr)
    monkeypatch.setattr(pe, "request_copilot_review", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    asyncio.run(
        pe.run_pr_endgame(
            spec_dir=spec,
            spec_id="001-x",
            worktree=spec,
            branch="aifactory/001-x",
            base="main",
            repo="owner/repo",
            auto_merge=False,
            review_tier="low",
            reviewer="copilot",
            background=False,
            trusted=_rec(signed),
        )
    )
    assert "Possible tampering" in captured["body"]
