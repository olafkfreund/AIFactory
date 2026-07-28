"""Orphan-task detection.

The test that matters most is the one asserting a `human_review` task is NEVER
stale, however old. Reaping on age alone would destroy real work waiting for a
person -- three tasks on the live board had waited 19, 28 and 38 hours
legitimately when this was written, alongside one genuine orphan at 27 hours.
Age alone cannot tell those apart; ownership can.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.services.stale_tasks import (  # noqa: E402
    DEFAULT_STALE_AFTER,
    find_stale,
    is_reapable,
    summarise,
)

_NOW = datetime(2026, 7, 29, 12, 0, 0)


def _task(status: str, hours_ago: float, task_id: str = "p:001-x") -> dict:
    return {
        "id": task_id,
        "status": status,
        "phase": "planning",
        "updated_at": (_NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def test_human_review_is_never_stale_however_old() -> None:
    """The single most important behaviour here.

    A person has not looked yet. That is not a fault, and a reaper that
    "fixes" it destroys work nobody agreed to abandon.
    """
    for age in (5, 38, 24 * 30):
        assert find_stale([_task("human_review", age)], now=_NOW) == []


def test_machine_owned_task_gone_quiet_is_stale() -> None:
    """The real case: in_progress, no worker, 27 hours of silence."""
    stale = find_stale([_task("in_progress", 27)], now=_NOW)
    assert len(stale) == 1
    assert stale[0].status == "in_progress"
    assert 26.9 < stale[0].idle_hours < 27.1
    # The reason must name the evidence, not just assert a verdict.
    assert "27.0h" in stale[0].reason()
    assert "machine-owned" in stale[0].reason()


def test_recent_machine_task_is_left_alone() -> None:
    """A slow build is not an orphan."""
    assert find_stale([_task("in_progress", 0.5)], now=_NOW) == []


def test_boundary_is_inclusive_so_a_stall_cannot_hover_below_it() -> None:
    hours = DEFAULT_STALE_AFTER.total_seconds() / 3600
    assert len(find_stale([_task("in_progress", hours)], now=_NOW)) == 1
    assert find_stale([_task("in_progress", hours - 0.01)], now=_NOW) == []


def test_terminal_states_are_never_stale() -> None:
    for status in ("done", "completed", "failed", "cancelled"):
        assert find_stale([_task(status, 999)], now=_NOW) == []


def test_unknown_status_is_not_reaped() -> None:
    """A status the reaper was never taught about must survive it.

    Guessing wrong in this direction deletes live work; guessing wrong in the
    other merely leaves an orphan visible, which is what we had anyway.
    """
    assert not is_reapable("some_future_state")
    assert find_stale([_task("some_future_state", 999)], now=_NOW) == []


def test_unparseable_timestamp_is_not_evidence_of_death() -> None:
    bad = {"id": "p:001", "status": "in_progress", "updated_at": "not-a-date"}
    assert find_stale([bad], now=_NOW) == []


def test_mixed_board_picks_only_the_orphan() -> None:
    """Modelled on the live board that prompted this."""
    board = [
        _task("in_progress", 27, "p:089-memory-chain-probe-three"),
        _task("human_review", 19, "p:090-memory-chain-probe-four"),
        _task("human_review", 28, "p:088-memory-chain-probe-two"),
        _task("human_review", 38, "p:087-memory-chain-probe"),
    ]
    stale = find_stale(board, now=_NOW)
    assert [s.task_id for s in stale] == ["p:089-memory-chain-probe-three"]


def test_summary_states_what_it_did() -> None:
    stale = find_stale([_task("in_progress", 27)], now=_NOW)
    dry = summarise(stale, dry_run=True)
    assert dry["action"] == "reported" and dry["stale_count"] == 1
    wet = summarise(stale, dry_run=False)
    assert wet["action"] == "marked failed"
    # A caller must be able to act on the report without re-deriving anything.
    assert wet["tasks"][0]["reason"]
