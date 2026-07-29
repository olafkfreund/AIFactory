"""Orphan-task detection.

The test that matters most is the one asserting a `human_review` task is NEVER
stale, however old. Reaping on age alone would destroy real work waiting for a
person -- three tasks on the live board had waited 19, 28 and 38 hours
legitimately when this was written, alongside one genuine orphan at 27 hours.
Age alone cannot tell those apart; ownership can.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.services.stale_tasks import (  # noqa: E402
    DEFAULT_STALE_AFTER,
    REAPED_STATUS,
    TERMINAL_STATES,
    find_stale,
    is_reapable,
    summarise,
)

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


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
    # The report must name the status actually written, or a caller reading
    # it learns the wrong thing about the board.
    assert wet["action"] == f"marked {REAPED_STATUS}"
    # A caller must be able to act on the report without re-deriving anything.
    assert wet["tasks"][0]["reason"]


def test_the_route_module_imports() -> None:
    """Import the ROUTE, not just the pure helper it wraps.

    The first version of this change imported `resolve_project_path` from
    task_service.py when it lives in projects.py. Every test in the repository
    failed, because a bad import in any registered route breaks app startup --
    and none of them named the route, so the cause was three screens of
    identical ImportErrors away from the file that caused it.

    Testing only the pure module could not catch that: the pure module has no
    such imports. This asserts the wiring, which is the part that broke.
    """
    # importorskip skips on a MISSING PACKAGE (an environment gap) but lets a
    # wrong NAME surface as ImportError, which is the bug this test exists for.
    stale = pytest.importorskip("server.routes.stale")

    assert stale.router is not None
    assert any(
        getattr(r, "path", "") == "/api/maintenance/stale-tasks"
        for r in stale.router.routes
    )


def test_the_route_is_not_shadowed_by_another_router() -> None:
    """Assert which route the APP resolves, not what the handler returns.

    The first version of this lived at /api/tasks/stale. tasks.py mounts
    `@router.get("/{task_id}")` under an /api/tasks prefix and is registered
    first, so FastAPI matched "stale" as a task id and the deployed endpoint
    answered `400 Invalid task ID format`. It was live and broken.

    Two earlier attempts at this test were useless: checking the ROUTER
    contains the path (the router was fine, the app's ordering was not), and
    checking the RESPONSE (both paths return 500 here for want of a workspace,
    so the assertion held either way). Resolution is the thing that broke, so
    resolution is what this asserts.
    """
    # importorskip rather than try/except+skip: it returns the module, so there
    # is no code path where the name is unbound. CodeQL flagged the earlier
    # form as py/uninitialized-local-variable because it does not model
    # pytest.skip as terminating - and it was right that the path existed.
    main = pytest.importorskip("server.main")

    app = main.create_app()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/maintenance/stale-tasks",
        "headers": [],
        "root_path": "",
    }
    matched = [
        r
        for r in app.router.routes
        if r.matches(scope)[0].value >= 2  # Match.FULL
    ]
    assert matched, "no route resolves /api/maintenance/stale-tasks"
    # The FIRST match is what the app dispatches to.
    assert matched[0].endpoint.__name__ == "report_stale", (
        f"shadowed by {matched[0].path} -> {matched[0].endpoint.__name__}"
    )


def test_endpoint_requires_no_undocumented_query_parameter() -> None:
    """The third distinct wiring failure in this feature, so it gets a test.

    `require_task_access` is task-scoped: it reads a `task_id`, and on a path
    that has no such path parameter FastAPI promotes it to a REQUIRED QUERY
    parameter. The deployed endpoint answered

        422 {"loc": ["query", "task_id"], "msg": "Field required"}

    and could not be called at all. It was registered, resolvable, and unusable
    -- which no test asserting registration or resolution could see.

    Asserts against the schema the app generates, so it covers parameters
    contributed by dependencies rather than only those written in the signature.
    """
    main = pytest.importorskip("server.main")

    schema = main.create_app().openapi()
    params = schema["paths"]["/api/maintenance/stale-tasks"]["get"].get(
        "parameters", []
    )
    required = {p["name"] for p in params if p.get("required")}
    assert required == set(), f"endpoint demands query parameters: {sorted(required)}"


def test_reaping_actually_writes_both_stores(tmp_path: Path) -> None:
    """The reap must CHANGE something, and change it in both places.

    The first implementation logged, appended the id to a `reaped` list, and
    returned `"action": "marked failed"` -- while writing nothing at all. It
    reported success and left the task exactly as it found it, which is the
    precise defect this whole feature exists to detect.

    Both stores matter: implementation_plan.json is what the task list renders
    from, task_control.json is authoritative for the board column (#259).
    Writing one and not the other leaves them disagreeing.
    """
    stale_mod = pytest.importorskip("server.routes.stale")

    spec = tmp_path / "001-orphan"
    spec.mkdir(parents=True)
    (spec / "implementation_plan.json").write_text(
        json.dumps({"status": "in_progress", "title": "orphan"})
    )

    stale_mod._mark_cancelled(spec, "orphaned: no worker for 27h")

    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["status"] == "cancelled", "plan file still says in_progress"
    assert "orphaned" in plan["reviewReason"]

    control = json.loads((spec / "task_control.json").read_text())
    assert control["status"] == "cancelled", "control store still says in_progress"
    assert control.get("updatedBy") == "stale-task-reaper"


def test_reaped_status_is_terminal_so_it_is_not_reaped_twice() -> None:
    """A reaped task must not come back round the loop.

    If the status written were still machine-owned, every sweep would re-reap
    the same tasks forever and the report would never settle.
    """
    assert REAPED_STATUS in TERMINAL_STATES
    assert not is_reapable(REAPED_STATUS)
