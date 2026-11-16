"""``decide_merge`` must actually gate the merge (#637), and fail closed.

``merge.merge_policy.decide_merge`` is the RFC-0011/RFC-0013 decision matrix: it
enforces the TFactory verdict, the RFC-0006 VAL floor and RFC-0009 CI parity on
top of the tier. Across every repo it appeared only in ``merge/__init__`` and in
its own tests -- the real merge path asked ``tier_permits_auto_merge``, which
checks the tier CEILING and nothing else. So a PR whose TFactory verdict was a
``handback`` auto-merged exactly like a clean one.

These tests assert through ``run_pr_endgame`` -- the seam every route into the
endgame passes through -- not on the pure function. A test proving
``decide_merge`` exists and returns the right string passes with the function
still unwired, which is the entire defect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_WS = Path(__file__).resolve().parents[1]
_BACKEND = _WS.parents[0] / "backend"
for _p in (str(_WS), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server.services.handback_receipt_store as hrs  # noqa: E402
from merge import merge_policy  # noqa: E402
from server.database.models import Base  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402

# Captured before conftest's autouse stub replaces it (#1696).
_REAL_ON_RECORD = hrs.on_record


@pytest.fixture(autouse=True)
def _isolated_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests model a legacy task on an isolated host (#1667, D4-i)."""
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    # kubejob counts as isolated only with the durable store (#1667).
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: True)
    # #1795: the project's settings are loaded, as on a running server.
    monkeypatch.setattr(
        "server.services.project_settings_store.is_loaded", lambda _p: True
    )


def _spec(tmp_path: Path, **meta: object) -> Path:
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    if meta:
        (spec / "task_metadata.json").write_text(json.dumps(meta))
    return spec


async def _endgame_auto_merge(
    spec: Path,
    tier: str | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    both: bool = False,
) -> bool | tuple[bool, bool]:
    """Run the endgame far enough to capture the auto_merge it settled on.

    ``both`` also returns the ``human_approval_required`` handed to the watcher.
    """
    captured: dict[str, bool] = {}

    async def _fake_watch(**kwargs: object) -> dict[str, object]:
        captured["auto_merge"] = bool(kwargs["auto_merge"])
        captured["human_approval_required"] = bool(
            kwargs.get("human_approval_required")
        )
        return {"merged": False, "reason": "stub"}

    monkeypatch.setattr(pe, "watch_and_finish", _fake_watch)
    monkeypatch.setattr(pe, "create_pr", lambda **_k: 42)
    monkeypatch.setattr(pe, "request_copilot_review", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))

    await pe.run_pr_endgame(
        spec_dir=spec,
        spec_id="001-x",
        worktree=spec,
        branch="aifactory/001-x",
        base="main",
        repo="owner/repo",
        auto_merge=True,
        review_tier=tier,
        reviewer="copilot",
        background=False,
        trusted=None,
    )
    if both:
        return captured["auto_merge"], captured["human_approval_required"]
    return captured["auto_merge"]


# --------------------------------------------------------------------------- #
# The headline: the decision matrix is consulted on the REAL path
# --------------------------------------------------------------------------- #


async def test_a_tfactory_handback_withholds_the_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole: an `auto` tier merged on the tier ceiling alone, so a build
    TFactory had HANDED BACK auto-merged like a verified one."""
    spec = _spec(tmp_path)
    (spec / "handback_received.json").write_text(
        json.dumps({"failing_test_count": 4, "correlation_key": "k"})
    )
    assert await _endgame_auto_merge(spec, "auto", monkeypatch) is False


async def test_the_same_task_without_the_handback_still_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. Without it the test above passes on any change that simply
    switches auto-merge off."""
    spec = _spec(tmp_path)
    assert await _endgame_auto_merge(spec, "auto", monkeypatch) is True


async def test_a_recorded_failing_verdict_withholds_the_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path, tfactoryVerdict="fail")
    assert await _endgame_auto_merge(spec, "auto", monkeypatch) is False


async def test_an_achieved_val_below_the_floor_withholds_the_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path, achievedVal="VAL-1", valFloor="VAL-3")
    assert await _endgame_auto_merge(spec, "auto", monkeypatch) is False


async def test_a_declared_val_floor_with_nothing_achieved_withholds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unmeasured default is VAL-0, so a declared floor is never waived."""
    spec = _spec(tmp_path, valFloor="VAL-2")
    assert await _endgame_auto_merge(spec, "auto", monkeypatch) is False


async def test_a_recorded_ci_parity_failure_withholds_the_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path, ciParity=False)
    assert await _endgame_auto_merge(spec, "auto", monkeypatch) is False


async def test_the_tier_ceiling_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1158 must not regress: decide_merge subsumes the ceiling it replaced."""
    spec = _spec(tmp_path)
    assert await _endgame_auto_merge(spec, "blocking", monkeypatch) is False
    assert await _endgame_auto_merge(spec, "nonsense", monkeypatch) is False


# --------------------------------------------------------------------------- #
# Fail closed, not open
# --------------------------------------------------------------------------- #


def test_the_gate_fails_closed_when_the_policy_module_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It returned True ("no opinion") on ImportError, so the one failure that
    removes the gate also removed the gate's ability to say no."""
    monkeypatch.setitem(sys.modules, "merge.merge_policy", None)
    assert (
        pe.merge_disposition(tmp_path, "auto", trusted=None)
        == pe.HOLD_BLOCKING_DISPOSITION
    )
    assert (
        pe.merge_disposition(tmp_path, None, trusted=None)
        == pe.HOLD_BLOCKING_DISPOSITION
    )


def test_the_disposition_literals_track_the_policy_module() -> None:
    """The literals are duplicated so the comparison survives an ImportError;
    this is what stops that duplication from drifting into "never merge"."""
    assert pe.AUTO_MERGE_DISPOSITION == merge_policy.AUTO_MERGE
    assert pe.HOLD_BLOCKING_DISPOSITION == merge_policy.HOLD_BLOCKING


def test_the_path_risk_floor_fails_closed_when_the_policy_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ImportError used to return the tier untouched -- "we did not look" is
    not "we looked and it was fine"."""
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    monkeypatch.setitem(sys.modules, "merge.merge_policy", None)
    spec = _spec(tmp_path, reviewTier="auto")

    tier, floor = pe.apply_path_risk_floor(
        tmp_path, spec, "001-x", "main", "auto", trusted=None
    )

    assert (tier, floor) == ("blocking", "blocking")
    assert (
        json.loads((spec / "task_metadata.json").read_text())["reviewTier"]
        == "blocking"
    )


def test_the_advisory_rollout_is_still_honoured_on_the_closed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing closed must not switch the rollout flag on behind the operator."""
    monkeypatch.delenv(pe.PATH_RISK_FLOOR_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "merge.merge_policy", None)
    spec = _spec(tmp_path, reviewTier="auto")

    tier, floor = pe.apply_path_risk_floor(
        tmp_path, spec, "001-x", "main", "auto", trusted=None
    )

    assert (tier, floor) == ("auto", "blocking")


# --------------------------------------------------------------------------- #
# A required gate has to be satisfiable, or the floor never lifts
# --------------------------------------------------------------------------- #


def test_recorded_approvals_do_not_lift_the_deployment_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task metadata is agent-writable, so it is not gate evidence (#1663): the
    floor sees no evidence and a declared gate keeps the tier floored."""
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    spec = _spec(tmp_path, reviewTier="auto", satisfiedSystemGates=["human-approval"])
    # #1984: the contract comes from context/task_contract.json only.
    (spec / "context").mkdir()
    (spec / "context" / "task_contract.json").write_text(
        json.dumps(
            {
                "contract_version": "2",
                "deployment": {"system_gates": ["human-approval"]},
            }
        )
    )
    monkeypatch.setattr(pe, "task_repo_dir", lambda *_a, **_k: None)

    tier, _floor = pe.apply_path_risk_floor(
        tmp_path, spec, "001-x", "main", "auto", trusted=None
    )

    assert tier == "blocking"


def test_an_outstanding_gate_still_floors_the_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above."""
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    spec = _spec(tmp_path, reviewTier="auto")
    # #1984: the contract comes from context/task_contract.json only.
    (spec / "context").mkdir()
    (spec / "context" / "task_contract.json").write_text(
        json.dumps(
            {
                "contract_version": "2",
                "deployment": {"system_gates": ["human-approval"]},
            }
        )
    )
    monkeypatch.setattr(pe, "task_repo_dir", lambda *_a, **_k: None)

    tier, _floor = pe.apply_path_risk_floor(
        tmp_path, spec, "001-x", "main", "auto", trusted=None
    )

    assert tier == "blocking"


# --------------------------------------------------------------------------- #
# The RFC-0013 deployment overlay holds on the live merge path (#1658)
# --------------------------------------------------------------------------- #

_GREEN = {
    "tfactoryVerdict": "pass",
    "achievedVal": 2,
    "valFloor": 1,
    "ciParity": True,
    "hostCiGreen": True,
}


def _contracted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract: object,
    **extra: object,
) -> Path:
    """A green-signal spec whose context/task_contract.json is ``contract``."""
    monkeypatch.delenv(pe.PATH_RISK_FLOOR_ENV, raising=False)
    spec = _spec(tmp_path, **_GREEN, **extra)
    (spec / "context").mkdir()
    text = contract if isinstance(contract, str) else json.dumps(contract)
    (spec / "context" / "task_contract.json").write_text(text)
    return spec


def _deployment(**dep: object) -> dict[str, object]:
    return {"contract_version": "2", "deployment": dep}


def test_a_production_deployment_holds_on_the_live_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _contracted(
        tmp_path, monkeypatch, _deployment(production_classification="production")
    )
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )


def test_a_high_risk_deployment_holds_on_the_live_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _contracted(tmp_path, monkeypatch, _deployment(risk_class="high"))
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )


def test_a_satisfied_system_gate_clears_only_its_own_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep = _deployment(system_gates=["human-approval"])
    held = _contracted(tmp_path / "a", monkeypatch, dep)
    assert (
        pe.merge_disposition(held, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )
    cleared = _contracted(tmp_path / "b", monkeypatch, dep)
    assert (
        pe.merge_disposition(
            cleared,
            "low",
            trusted=None,
            satisfied_gates=["human-approval"],
            fetch_signals=lambda: _PASS,
        )
        == pe.AUTO_MERGE_DISPOSITION
    )


def test_a_contract_cannot_satisfy_its_own_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _contracted(
        tmp_path,
        monkeypatch,
        _deployment(
            system_gates=["human-approval"], satisfied_gates=["human-approval"]
        ),
    )
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )


def test_task_metadata_is_not_gate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _contracted(
        tmp_path,
        monkeypatch,
        _deployment(system_gates=["human-approval"]),
        satisfiedSystemGates=["human-approval"],
    )
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )


def test_merge_disposition_takes_explicit_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _contracted(
        tmp_path, monkeypatch, _deployment(system_gates=["human-approval"])
    )
    assert (
        pe.merge_disposition(
            spec,
            "low",
            trusted=None,
            satisfied_gates=["human-approval"],
            fetch_signals=lambda: _PASS,
        )
        == pe.AUTO_MERGE_DISPOSITION
    )


async def test_a_human_only_hold_keeps_the_watcher_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = _contracted(
        tmp_path / "a", monkeypatch, _deployment(system_gates=["human-approval"])
    )
    assert await _endgame_auto_merge(gated, "auto", monkeypatch, both=True) == (
        True,
        True,
    )
    prod = _contracted(
        tmp_path / "b",
        monkeypatch,
        _deployment(production_classification="production"),
    )
    assert await _endgame_auto_merge(prod, "auto", monkeypatch, both=True) == (
        False,
        False,
    )
    high = _contracted(
        tmp_path / "c",
        monkeypatch,
        _deployment(system_gates=["human-approval"], risk_class="high"),
    )
    assert await _endgame_auto_merge(high, "auto", monkeypatch, both=True) == (
        False,
        False,
    )


@pytest.mark.parametrize("text", ["{not json", "[]"])
def test_an_unreadable_contract_holds_on_the_live_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    spec = _contracted(tmp_path, monkeypatch, text)
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )


def test_a_satisfied_gate_never_clears_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _contracted(
        tmp_path, monkeypatch, _deployment(production_classification="production")
    )
    assert (
        pe.merge_disposition(
            spec, "low", trusted=None, satisfied_gates=["human-approval"]
        )
        == pe.HOLD_BLOCKING_DISPOSITION
    )


# --------------------------------------------------------------------------- #
# The background watcher must survive the garbage collector
# --------------------------------------------------------------------------- #


async def test_the_background_watcher_is_strongly_referenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asyncio holds only a WEAK reference to a running task, so a bare
    create_task(coro) can be collected mid-flight -- no exception, no log --
    after the API has already answered {"ok": true, "watching": true}."""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def _fake_watch(**_kwargs: object) -> dict[str, object]:
        started.set()
        await asyncio.sleep(0)
        finished.set()
        return {"merged": False}

    monkeypatch.setattr(pe, "watch_and_finish", _fake_watch)
    monkeypatch.setattr(pe, "create_pr", lambda **_k: 7)
    monkeypatch.setattr(pe, "request_copilot_review", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    pe._BACKGROUND_TASKS.clear()

    res = await pe.run_pr_endgame(
        spec_dir=_spec(tmp_path),
        spec_id="001-x",
        worktree=tmp_path,
        branch="aifactory/001-x",
        base="main",
        repo="owner/repo",
        auto_merge=False,
        reviewer="copilot",
        background=True,
        trusted=None,
    )

    assert res["watching"] is True
    # The reference exists while the watcher runs...
    assert len(pe._BACKGROUND_TASKS) == 1
    await asyncio.wait_for(finished.wait(), timeout=5)
    await asyncio.sleep(0)
    # ...and is released when it completes, so the set cannot grow unbounded.
    assert not pe._BACKGROUND_TASKS
    assert started.is_set()


@pytest.mark.parametrize(
    "where",
    # #1984: implementation_plan.json is agent-written and never a contract source.
    [("context", "task_contract.json")],
)
def test_a_contract_that_raises_on_read_holds_instead(
    tmp_path: Path, where: tuple[str, ...]
) -> None:
    """#1658 review: a nested-past-the-recursion-limit or non-UTF-8 contract
    raised out of merge_disposition, aborting the endgame before the PR opened."""
    spec = _spec(tmp_path)
    target = spec.joinpath(*where)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[" * 100_000)
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )
    target.write_bytes(b"\xff\xfe not utf-8")
    assert (
        pe.merge_disposition(spec, "low", trusted=None) == pe.HOLD_BLOCKING_DISPOSITION
    )


async def _watch_once(**kw: object) -> dict[str, Any]:
    result: dict[str, Any] = await pe.watch_and_finish(
        owner="o",
        repo="r",
        pr=7,
        auto_merge=True,
        review_fn=lambda: pe.ReviewState("changes_requested"),
        poll_interval=0,
        max_minutes=1,
        **kw,
    )
    return result


@pytest.mark.parametrize(
    ("configured", "required", "warns"),
    [(False, True, True), (True, True, False), (False, False, False)],
    ids=["human_gate_without_app_warns", "with_app_silent", "no_human_gate_silent"],
)
async def test_human_gate_warning(
    configured: bool,
    required: bool,
    warns: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(pe.github_app, "configured", lambda: configured)
    with caplog.at_level(logging.WARNING, logger=pe.logger.name):
        await _watch_once(human_approval_required=required)
    hits = [r for r in caplog.records if "no GitHub App configured" in r.getMessage()]
    assert len(hits) == (1 if warns else 0)


# --------------------------------------------------------------------------- #
# #1695: fetched (trusted) signals; metadata can only tighten them
# --------------------------------------------------------------------------- #

_PASS = {"tfactory_verdict": "pass", "host_ci_green": True}


def test_1695_recorded_fail_tightens_a_fetched_pass(tmp_path: Path) -> None:
    spec = _spec(tmp_path, tfactoryVerdict="fail")
    assert pe.merge_gate_signals(spec, fetched=_PASS)["tfactory_verdict"] == "fail"


def test_1695_handback_receipt_beats_the_fetched_verdict(tmp_path: Path) -> None:
    spec = _spec(tmp_path, tfactoryVerdict="fail")
    (spec / pe._HANDBACK_RECEIPT).write_text("{}")
    out = pe.merge_gate_signals(spec, fetched=_PASS)
    assert out["tfactory_verdict"] == "handback"


def test_1695_recorded_pass_cannot_clear_a_fetched_fail(tmp_path: Path) -> None:
    spec = _spec(tmp_path, tfactoryVerdict="pass")
    fetched = {"tfactory_verdict": "fail", "host_ci_green": True}
    assert pe.merge_gate_signals(spec, fetched=fetched)["tfactory_verdict"] == "fail"


@pytest.mark.parametrize(
    "fetched", [{"tfactory_verdict": None, "host_ci_green": None}, {}]
)
def test_1695_missing_fetched_values_fail_closed(
    tmp_path: Path, fetched: dict[str, object]
) -> None:
    out = pe.merge_gate_signals(_spec(tmp_path), fetched=fetched)
    assert out["host_ci_green"] is False
    assert out["tfactory_verdict"] is None


def test_1695_recorded_host_ci_false_tightens(tmp_path: Path) -> None:
    spec = _spec(tmp_path, hostCiGreen=False)
    assert pe.merge_gate_signals(spec, fetched=_PASS)["host_ci_green"] is False


def test_1695_recorded_host_ci_true_cannot_clear_an_unknown(tmp_path: Path) -> None:
    spec = _spec(tmp_path, hostCiGreen=True)
    fetched = {"tfactory_verdict": "pass", "host_ci_green": None}
    assert pe.merge_gate_signals(spec, fetched=fetched)["host_ci_green"] is False


def test_1697_metadata_greens_do_not_loosen(tmp_path: Path) -> None:
    out = pe.merge_gate_signals(_spec(tmp_path, **_GREEN))
    assert out["tfactory_verdict"] is None
    assert out["host_ci_green"] is False


def test_1697_no_fetch_is_not_passed(tmp_path: Path) -> None:
    assert pe.merge_gate_signals(_spec(tmp_path)) == {
        "host_ci_green": False,
        "tfactory_verdict": None,
        "achieved_val": 0,
        "val_floor": None,
        "ci_parity": True,
    }


# --------------------------------------------------------------------------- #
# #1696: the DB receipt holds after the QA Fixer deletes the file
# --------------------------------------------------------------------------- #


async def _receipt_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        "server.database.engine.async_session_factory",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    monkeypatch.setattr(hrs, "on_record", _REAL_ON_RECORD)
    return engine


async def test_1696_deleted_receipt_file_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _receipt_db(tmp_path, monkeypatch)
    try:
        spec = _spec(tmp_path)
        receipt = spec / pe._HANDBACK_RECEIPT
        receipt.write_text("{}")
        await hrs.record(spec)
        receipt.unlink()  # what a QA Fixer on the control-plane spec dir can do
        assert await _endgame_auto_merge(spec, "auto", monkeypatch) is False
    finally:
        await engine.dispose()


async def test_1696_no_receipt_row_still_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _receipt_db(tmp_path, monkeypatch)
    try:
        assert await _endgame_auto_merge(_spec(tmp_path), "auto", monkeypatch) is True
    finally:
        await engine.dispose()


@pytest.mark.parametrize(("held", "expected"), [(True, False), (False, True)])
async def test_1696_merge_time_gate_checks_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool, expected: bool
) -> None:
    async def _on_record(_spec_dir: object) -> bool:
        return held

    monkeypatch.setattr(hrs, "on_record", _on_record)
    gate = pe._with_receipt_veto(
        lambda _h: True, _spec(tmp_path), asyncio.get_running_loop()
    )
    assert await asyncio.to_thread(gate, "deadbeef") is expected


# --------------------------------------------------------------------------- #
# #1963: a passing verdict cannot merge past a red deterministic floor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("host_ci", "late_meta", "merges"),
    [
        (False, None, False),
        (True, {"achievedVal": "VAL-1", "valFloor": "VAL-3"}, False),
        (True, {"ciParity": False}, False),
        (True, None, True),
    ],
    ids=["host_ci_red", "val_below_floor", "ci_parity_false", "all_green"],
)
async def test_1963_a_pass_verdict_cannot_merge_past_a_red_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_ci: bool,
    late_meta: dict[str, object] | None,
    merges: bool,
) -> None:
    """The permissive model verdict is evidence, and the floor decides (Factory#1963)."""
    spec = _spec(tmp_path)
    calls: list[object] = []

    def _merge(*_a: object, **k: object) -> bool:
        calls.append(k.get("match_head"))
        return True

    def _create_pr(**_k: object) -> int:
        if late_meta is not None:
            (spec / "task_metadata.json").write_text(json.dumps(late_meta))
        return 42

    monkeypatch.setattr(pe, "merge_pr", _merge)
    monkeypatch.setattr(pe, "create_pr", _create_pr)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    monkeypatch.setattr(
        "pfactory.tfactory_client.tfactory_verdict", lambda *_a, **_k: "pass"
    )
    monkeypatch.setattr(pe, "host_ci_green", lambda *_a, **_k: host_ci)
    monkeypatch.setattr(pe, "required_checks_green", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_head", lambda *_a: "deadbeef")
    real = pe.watch_and_finish

    async def _watch(**kw: Any) -> dict[str, object]:
        out: dict[str, object] = await real(**kw, poll_interval=0, max_minutes=1)
        return out

    monkeypatch.setattr(pe, "watch_and_finish", _watch)

    result = await pe.run_pr_endgame(
        spec_dir=spec,
        spec_id="001-x",
        worktree=spec,
        branch="aifactory/001-x",
        base="main",
        repo="owner/repo",
        auto_merge=True,
        review_tier="auto",
        reviewer="aifactory",
        review_fn=lambda: pe.ReviewState("approved"),
        background=False,
        trusted=None,
    )
    if merges:
        assert calls == ["deadbeef"]
        assert result["merged"] is True
    else:
        assert calls == []
        assert result["merged"] is False
        assert result["reason"] == "merge_gate_timeout"


def test_endgame_hands_the_canonical_trailer_block_to_the_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory#1960: the squash body comes from the canonical spec dir."""
    monkeypatch.setenv("AIFACTORY_AGENT_COMMIT_TRAILERS", "1")
    spec = _spec(tmp_path)
    (spec / "requirements.json").write_text(
        json.dumps({"agent_identity": "agent:aifactory/k1", "originator": "human:x"})
    )
    captured: dict[str, object] = {}

    async def _fake_watch(**kwargs: object) -> dict[str, object]:
        captured["merge_body"] = kwargs.get("merge_body")
        return {"merged": False, "reason": "stub"}

    monkeypatch.setattr(pe, "watch_and_finish", _fake_watch)
    monkeypatch.setattr(pe, "create_pr", lambda **_k: 42)
    monkeypatch.setattr(pe, "request_copilot_review", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    asyncio.run(
        pe.run_pr_endgame(
            spec_dir=spec,
            spec_id="001-x",
            worktree=tmp_path / "wt",
            branch="aifactory/001-x",
            base="main",
            repo="owner/repo",
            auto_merge=True,
            reviewer="copilot",
            background=False,
            trusted=None,
        )
    )
    assert captured["merge_body"] == (
        "Agent-Identity: agent:aifactory/k1\nOriginator: human:x"
    )
