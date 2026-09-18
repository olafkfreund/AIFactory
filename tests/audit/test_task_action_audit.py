"""Task actions on the REST surface write an audit row (#1466).

Before this, the only task-action audit sink was the MCP proxy, so the same
action through ``/api/tasks/*`` left no row at all. The rule under test: one
row per action whichever surface it came through -- ``task.*`` from the REST
handler, ``mcp.task.*`` from the proxy, never both.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from types import SimpleNamespace

import pytest
from server.routes import execution, plan_approval, pr, tasks, worktree_merge
from server.services import audit_service

pytestmark = pytest.mark.audit

USER = {"id": "u-1", "org_id": "o-1"}


@pytest.fixture
def rows(monkeypatch):
    """Capture every row the background audit sink would write."""
    written: list[dict] = []

    async def _sink(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event_bg", _sink)
    return written


def _run(coro):
    return asyncio.run(coro)


# -- the helper --------------------------------------------------------------


def test_helper_writes_one_attributed_row(rows):
    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.9"))
    _run(audit_service.audit_task_action(USER, "task.stop", "p:001", req, {"a": 1}))
    assert rows == [
        {
            "user_id": "u-1",
            "org_id": "o-1",
            "action": "task.stop",
            "resource_type": "task",
            "resource_id": "p:001",
            "details": {"a": 1},
            "ip": "10.0.0.9",
        }
    ]


def test_helper_skips_a_proxied_call(rows):
    # A direct in-process call leaves `_access` at its Depends(...) default.
    default = inspect.signature(execution.stop_task).parameters["_access"].default
    _run(audit_service.audit_task_action(default, "task.stop", "p:001"))
    assert rows == []


def test_a_failing_sink_never_breaks_the_action(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_service.engine, "async_session_factory", _boom)
    # log_audit_event_bg swallows its own failure; the helper must not raise.
    _run(audit_service.audit_task_action(USER, "task.stop", "p:001"))


# -- one row per surface -----------------------------------------------------


@pytest.fixture
def stoppable(monkeypatch):
    async def _stop(_task_id):
        return True

    async def _emit(*_a, **_k):
        return None

    svc = SimpleNamespace(is_running=lambda _t: True, stop_task=_stop)
    monkeypatch.setattr(execution, "get_agent_service", lambda: svc)
    monkeypatch.setattr(execution, "emit_task_status", _emit)


def test_rest_stop_writes_one_task_row(rows, stoppable):
    _run(execution.stop_task("p:001", _access=USER))
    assert [r["action"] for r in rows] == ["task.stop"]


def test_proxied_stop_writes_only_the_mcp_row(rows, stoppable, monkeypatch):
    # The package re-exports its APIRouter as `router`, shadowing the module.
    mcp_router = importlib.import_module("server.mcp_stdio.router")
    # It imported the sink by name, so the `rows` patch does not reach it.
    monkeypatch.setattr(
        mcp_router, "log_audit_event_bg", audit_service.log_audit_event_bg
    )

    req = SimpleNamespace(client=None)
    key = SimpleNamespace(user_id="u-1")
    _run(mcp_router.proxy_stop_task("p:001", req, key=key))
    assert [r["action"] for r in rows] == ["mcp.task.stop"]


def test_rest_delete_writes_one_task_row(rows, monkeypatch, tmp_path):
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    monkeypatch.setattr(tasks, "load_projects", lambda: {"p": {"path": str(tmp_path)}})
    _run(tasks.delete_task("p:001-x", _access=USER))
    assert not spec.exists()
    assert [(r["action"], r["resource_id"]) for r in rows] == [
        ("task.delete", "p:001-x")
    ]


# -- the thin wrappers around many-return bodies -----------------------------


@pytest.mark.parametrize(
    ("module", "route", "body", "action"),
    [
        (pr, "create_pr_from_task", "_create_pr_from_task", "task.create_pr"),
        (worktree_merge, "merge_worktree", "_merge_worktree", "task.merge"),
    ],
)
@pytest.mark.parametrize(("success", "expected"), [(True, 1), (False, 0)])
def test_wrapper_audits_only_a_reported_success(
    rows, monkeypatch, module, route, body, action, success, expected
):
    async def _body(*_a, **_k):
        return {"success": success}

    monkeypatch.setattr(module, body, _body)
    _run(getattr(module, route)("p:001", None, _access=USER))
    assert [r["action"] for r in rows] == [action] * expected


def test_start_wrapper_audits(rows, monkeypatch):
    async def _body(*_a, **_k):
        return {"success": True}

    monkeypatch.setattr(execution, "_start_task", _body)
    req = SimpleNamespace(client=None)
    _run(execution.start_task("p:001", None, req, _access=USER))
    assert [r["action"] for r in rows] == ["task.start"]


# -- every audited route names its action ------------------------------------
# ponytail: a source check, not a live call per route -- each remaining route
# needs its own project/agent fixture to reach its success path. The live tests
# above cover the helper, both surfaces and the wrappers; this catches a route
# losing its call.


@pytest.mark.parametrize(
    ("fn", "action"),
    [
        (execution.handoff_to_tfactory, "ACTION_TASK_HANDOFF"),
        (execution.recover_task, "ACTION_TASK_RECOVER"),
        (execution.create_and_run_task, "ACTION_TASK_CREATE"),
        (execution.create_from_trusted_plan, "ACTION_TASK_CREATE"),
        (execution.apply_task_correction, "ACTION_TASK_APPLY_CORRECTION"),
        (execution.dispatch_task_to_copilot, "ACTION_TASK_DISPATCH"),
        (plan_approval.approve_plan, "ACTION_TASK_APPROVE_PLAN"),
        (tasks.create_task, "ACTION_TASK_CREATE"),
        (tasks.update_task_status, "ACTION_TASK_UPDATE"),
        (tasks.update_task, "ACTION_TASK_UPDATE"),
    ],
)
def test_route_emits_its_action(fn, action):
    src = inspect.getsource(inspect.unwrap(fn))
    assert "audit_task_action(" in src and action in src
