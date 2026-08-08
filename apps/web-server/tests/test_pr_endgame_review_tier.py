"""reviewTier must actually gate the merge (#1158).

`intake.build_execution_block` sets `review_tier` per RFC-0011 tier
(low -> auto, medium -> async, hard -> blocking) and
`trusted_plan._EXECUTION_TO_METADATA` carries it into `task_metadata.json` as
`reviewTier`. Nothing read it back, and `merge/merge_policy.py` -- the function
that would consume it -- had no non-test caller anywhere in `apps/`. So the
RFC-0011 promise ("a low task auto-merges, a hard task holds for a blocking
human review") was not in effect: every tier got the same fleet-wide
`AIFACTORY_AUTO_MERGE` boolean.

The tier now NARROWS that flag inside `run_pr_endgame`, and can only ever make
it stricter.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
_BACKEND = _WS.parents[0] / "backend"
for _p in (str(_WS), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from merge.merge_policy import tier_permits_auto_merge  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402


def _spec(tmp_path: Path, tier: object) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    meta: dict = {"model": "sonnet"}
    if tier is not None:
        meta["reviewTier"] = tier
    (spec / "task_metadata.json").write_text(json.dumps(meta))
    return spec


# ---------------------------------------------------------------------------
# The tier ceiling itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["low", "auto", " AUTO "])
def test_low_tier_permits_auto_merge(tier: str) -> None:
    assert tier_permits_auto_merge(tier) is True


@pytest.mark.parametrize("tier", ["medium", "async", "hard", "blocking"])
def test_medium_and_hard_tiers_forbid_auto_merge(tier: str) -> None:
    assert tier_permits_auto_merge(tier) is False


@pytest.mark.parametrize("tier", [None, "", "   "])
def test_absent_tier_leaves_the_caller_unchanged(tier: object) -> None:
    """Back-compat: a task with no reviewTier must behave exactly as before,
    the same rule the RFC-0013 deployment overlay uses for absent inputs."""
    assert tier_permits_auto_merge(tier) is True  # type: ignore[arg-type]


def test_unreadable_tier_is_not_a_licence_to_merge() -> None:
    """Matches decide_merge, which returns HOLD_BLOCKING for an unknown tier."""
    assert tier_permits_auto_merge("nonsense") is False


# ---------------------------------------------------------------------------
# Reading it off the task
# ---------------------------------------------------------------------------


def test_reads_review_tier_from_task_metadata(tmp_path: Path) -> None:
    assert pe.read_review_tier(_spec(tmp_path, "blocking")) == "blocking"


@pytest.mark.parametrize("bad", [None, 123, "", "  "])
def test_missing_or_malformed_tier_reads_as_none(tmp_path: Path, bad: object) -> None:
    assert pe.read_review_tier(_spec(tmp_path, bad)) is None


def test_no_metadata_file_reads_as_none(tmp_path: Path) -> None:
    assert pe.read_review_tier(tmp_path) is None
    assert pe.read_review_tier(None) is None


def test_corrupt_metadata_reads_as_none(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "task_metadata.json").write_text("{not json")
    assert pe.read_review_tier(spec) is None


# ---------------------------------------------------------------------------
# The gate, through run_pr_endgame -- the seam every caller passes through
# ---------------------------------------------------------------------------


async def _endgame(
    spec_dir: Path, *, auto_merge: bool, monkeypatch: pytest.MonkeyPatch
) -> bool:
    """Run the endgame far enough to capture the auto_merge it settled on."""
    captured: dict[str, bool] = {}

    async def _fake_watch(**kwargs: object) -> dict[str, object]:
        captured["auto_merge"] = bool(kwargs["auto_merge"])
        return {"merged": False, "reason": "stub"}

    def _fake_create_pr(**kwargs: object) -> int:
        assert kwargs["base"] == "main"
        return 42

    def _fake_request_review(
        owner: str, name: str, pr: int, runner: object = None
    ) -> bool:
        return bool(owner and name and pr and runner)

    def _fake_title_body(spec_dir: Path, spec_id: str) -> tuple[str, str]:
        return (spec_id, str(spec_dir))

    monkeypatch.setattr(pe, "watch_and_finish", _fake_watch)
    monkeypatch.setattr(pe, "create_pr", _fake_create_pr)
    monkeypatch.setattr(pe, "request_copilot_review", _fake_request_review)
    monkeypatch.setattr(pe, "_pr_title_body", _fake_title_body)

    await pe.run_pr_endgame(
        spec_dir=spec_dir,
        spec_id="001-x",
        worktree=spec_dir,
        branch="aifactory/001-x",
        base="main",
        repo="owner/repo",
        auto_merge=auto_merge,
        reviewer="copilot",
        background=False,
    )
    return captured["auto_merge"]


async def test_blocking_tier_withholds_auto_merge_despite_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #1158 regression: a factory:hard task auto-merged like any other."""
    assert (
        await _endgame(_spec(tmp_path, "blocking"), auto_merge=True, monkeypatch=monkeypatch)
        is False
    )


async def test_async_tier_withholds_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        await _endgame(_spec(tmp_path, "async"), auto_merge=True, monkeypatch=monkeypatch)
        is False
    )


async def test_auto_tier_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        await _endgame(_spec(tmp_path, "auto"), auto_merge=True, monkeypatch=monkeypatch)
        is True
    )


async def test_task_without_a_tier_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No behaviour change for every task that predates the tier."""
    assert (
        await _endgame(_spec(tmp_path, None), auto_merge=True, monkeypatch=monkeypatch)
        is True
    )


async def test_the_tier_can_only_tighten_never_widen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIFACTORY_AUTO_MERGE stays the master switch: an `auto` tier must NOT
    turn a merge on that the operator left off."""
    assert (
        await _endgame(_spec(tmp_path, "auto"), auto_merge=False, monkeypatch=monkeypatch)
        is False
    )
