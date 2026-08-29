"""The path-derived risk floor must reach the merge gate (#1456).

`review_tier.HIGH_RISK_PATTERNS` and the whole of `merge/merge_policy.py` were
both computed and discarded: the floor's only consumer was a `print()`, and
`decide_merge` / `deployment_block_reasons` had no caller outside their own
test. The tier that actually gated the merge came from a GitHub label — i.e. a
change declared its own risk.

These tests assert at the CALL SITE (`gather_pr_context`), not on the pure
helper: a test on `floor_from_paths` alone passes with the function still
unwired, which is precisely the defect being fixed.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
_BACKEND = _WS.parents[0] / "backend"
for _p in (str(_WS), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli import workspace_commands  # noqa: E402
from merge.merge_policy import floor_from_paths, raise_review_tier  # noqa: E402
from review_tier import HIGH_RISK_PATTERNS  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402


def _spec(
    tmp_path: Path, tier: str | None, *, deployment: dict[str, object] | None = None
) -> Path:
    """A spec dir + worktree shaped like a finished build."""
    spec_id = "001-x"
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / spec_id).mkdir(parents=True)
    spec = tmp_path / ".aifactory" / "specs" / spec_id
    spec.mkdir(parents=True)
    (spec / "requirements.json").write_text(
        json.dumps({"github_repo": "olafkfreund/AIFactory"})
    )
    meta: dict[str, object] = {"base_branch": "dev"}
    if tier is not None:
        meta["reviewTier"] = tier
    (spec / "task_metadata.json").write_text(json.dumps(meta))
    if deployment is not None:
        (spec / "context").mkdir()
        (spec / "context" / "task_contract.json").write_text(
            json.dumps({"contract_version": "1.0", "deployment": deployment})
        )
    return spec


def _runner(argv: list[str], _cwd: str | None = None) -> pe.CmdResult:
    if "rev-parse" in argv:
        return pe.CmdResult(0, "aifactory/001-x", "")
    return pe.CmdResult(1, "", "no")


def _changed(monkeypatch: pytest.MonkeyPatch, files: list[str]) -> None:
    """Stub the ONE differ the floor is wired to (#1089's pushed-ref reader)."""

    def _fake(
        _worktree: Path,
        base_branch: str = "main",
        _project_dir: Path | None = None,
        spec_name: str | None = None,
    ) -> list[str]:
        # The floor must read the pushed branch against origin/<base>, not the
        # control-plane worktree's HEAD — assert the contract, don't just stub.
        assert base_branch == "origin/dev"
        assert spec_name == "001-x"
        return files

    monkeypatch.setattr(workspace_commands, "_get_changed_files_from_git", _fake)


# --------------------------------------------------------------------------- #
# (a) an `auto` task touching a high-risk path is raised at the call site
# --------------------------------------------------------------------------- #


def test_auto_tier_touching_auth_is_floored_at_the_call_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spec = _spec(tmp_path, "auto")
    _changed(monkeypatch, ["app/auth/session.py"])

    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        ctx = pe.gather_pr_context(tmp_path, spec, "001-x", runner=_runner)

    assert ctx is not None
    assert ctx["review_tier_floor"] == "blocking"
    assert pe.PATH_RISK_FLOOR_NOTE in caplog.text
    # Advisory rollout: the finding is recorded, the gating tier is untouched.
    assert ctx["review_tier"] == "auto"
    meta = json.loads((spec / "task_metadata.json").read_text())
    assert meta["pathRiskFloor"] == "blocking"
    assert meta["reviewTier"] == "auto"


def test_enforcing_the_floor_withholds_the_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the rollout flag on, the SAME wiring reaches the merge gate and the
    tier written back for TFactory's VAL floor."""
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    spec = _spec(tmp_path, "auto")
    _changed(monkeypatch, ["app/auth/session.py"])

    ctx = pe.gather_pr_context(tmp_path, spec, "001-x", runner=_runner)

    assert ctx is not None
    assert ctx["review_tier"] == "blocking"
    assert pe.tier_allows_auto_merge(ctx["review_tier"]) is False
    written = json.loads((spec / "task_metadata.json").read_text())
    assert written["reviewTier"] == "blocking"


# --------------------------------------------------------------------------- #
# (b) never lowers
# --------------------------------------------------------------------------- #


def test_blocking_tier_with_unfloored_paths_stays_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    spec = _spec(tmp_path, "blocking")
    _changed(monkeypatch, ["README.md", "docs/guide.md"])

    ctx = pe.gather_pr_context(tmp_path, spec, "001-x", runner=_runner)

    assert ctx is not None
    assert ctx["review_tier"] == "blocking"
    assert ctx["review_tier_floor"] is None  # nothing changed, nothing to say
    assert pe.tier_allows_auto_merge(ctx["review_tier"]) is False


@pytest.mark.parametrize(
    ("tier", "floor"),
    [("blocking", "auto"), ("async", "auto"), ("hard", "low"), ("medium", "auto")],
)
def test_raise_review_tier_never_lowers(tier: str, floor: str) -> None:
    assert raise_review_tier(tier, floor) == tier


# --------------------------------------------------------------------------- #
# (c) the RFC-0013 deployment overlay is consulted
# --------------------------------------------------------------------------- #


def test_high_risk_deployment_floors_an_auto_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deployment_block_reasons had no caller outside its own test. A high
    risk_class must now floor the tier even when no path is high-risk."""
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    spec = _spec(tmp_path, "auto", deployment={"risk_class": "high"})
    _changed(monkeypatch, ["README.md"])

    ctx = pe.gather_pr_context(tmp_path, spec, "001-x", runner=_runner)

    assert ctx is not None
    assert ctx["review_tier"] == "blocking"
    assert pe.tier_allows_auto_merge(ctx["review_tier"]) is False


# --------------------------------------------------------------------------- #
# Back-compat: an unmeasurable diff must never raise a tier
# --------------------------------------------------------------------------- #


def test_unreadable_diff_leaves_the_tier_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(pe.PATH_RISK_FLOOR_ENV, "true")
    spec = _spec(tmp_path, "auto")

    def _boom(*_a: object, **_k: object) -> list[str]:
        raise OSError("no git here")

    monkeypatch.setattr(workspace_commands, "_get_changed_files_from_git", _boom)

    ctx = pe.gather_pr_context(tmp_path, spec, "001-x", runner=_runner)

    assert ctx is not None
    assert ctx["review_tier"] == "auto"
    assert ctx["review_tier_floor"] is None


def test_floor_reuses_the_shared_high_risk_table() -> None:
    """The table is imported from review_tier, never restated (Factory#590)."""
    for pattern in HIGH_RISK_PATTERNS:
        assert floor_from_paths([f"src/{pattern}/x.py"]) == "blocking"
    assert floor_from_paths(["README.md"]) == "auto"
    assert floor_from_paths([]) == "auto"
