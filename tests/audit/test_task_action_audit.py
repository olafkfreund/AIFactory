"""Task actions on the REST surface write an audit row (#1466).

Before this, the only task-action audit sink was the MCP proxy, so the same
action through ``/api/tasks/*`` left no row at all. The rule under test: one
row per action whichever surface it came through -- ``task.*`` from the REST
handler, ``mcp.task.*`` from the proxy, never both.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from server.routes import (
    execution,
    plan_approval,
    pr,
    projects,
    tasks,
    worktree_merge,
)
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


# -- @audit_task_route: the routes with many return sites ----------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [({"success": True}, 1), ({"success": False}, 0), ("not a dict", 0)],
)
def test_route_decorator_audits_only_a_reported_success(rows, result, expected):
    @audit_service.audit_task_route("task.merge")
    async def route(task_id, options=None, _access=None):
        return result

    assert _run(route("p:001", None, _access=USER)) == result
    assert [r["action"] for r in rows] == ["task.merge"] * expected


def test_route_decorator_skips_a_proxied_positional_call(rows):
    """The MCP proxy calls ``merge_worktree(task_id, options)`` directly:
    ``_access`` stays at its Depends default, so only the proxy's row exists."""
    marker = object()  # stands in for Depends(...)

    @audit_service.audit_task_route("task.merge")
    async def route(task_id, options=None, _access=marker):
        return {"success": True}

    _run(route("p:001", None))
    assert rows == []


@pytest.mark.parametrize(
    ("module", "route", "const"),
    [
        (execution, "start_task", "ACTION_TASK_START"),
        (pr, "create_pr_from_task", "ACTION_TASK_CREATE_PR"),
        (worktree_merge, "merge_worktree", "ACTION_TASK_MERGE"),
    ],
)
def test_many_return_routes_carry_the_decorator_under_honest_status(
    module, route, const
):
    """The body stays whole (so the #1126 guard still sees its refusals) and
    the audit decorator sits below ``@honest_status`` so it sees the raw dict."""
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == route
    )
    names = [ast.unparse(d) for d in fn.decorator_list]
    audit = f"audit_task_route({const})"
    assert audit in names, names
    if "honest_status" in names:
        assert names.index("honest_status") < names.index(audit), names


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
        (projects.create_project_task, "ACTION_TASK_CREATE"),
        (tasks.update_task_status, "ACTION_TASK_UPDATE"),
        (tasks.update_task, "ACTION_TASK_UPDATE"),
    ],
)
def test_route_emits_its_action(fn, action):
    src = inspect.getsource(inspect.unwrap(fn))
    assert "audit_task_action(" in src and action in src


# -- review follow-ups (Copilot on #1564): only an action that happened -------


@pytest.mark.parametrize(("sent", "expected"), [(True, 1), (False, 0)])
def test_handoff_audits_only_a_sent_handoff(
    rows, monkeypatch, tmp_path, sent, expected
):
    """``send_handoff`` reports a failed transport as ``sent: False``; no row."""
    import pfactory.tfactory_client as tc

    monkeypatch.setattr(
        execution, "load_projects", lambda: {"p": {"path": str(tmp_path)}}
    )
    (tmp_path / ".aifactory" / "specs" / "001").mkdir(parents=True)
    monkeypatch.setattr(tc, "build_ingest_payload", lambda *_a: {"spec_id": "001"})

    async def _send(_payload):
        return {"sent": sent, "reason": None if sent else "not_configured"}

    monkeypatch.setattr(tc, "send_handoff", _send)
    _run(execution.handoff_to_tfactory("p:001", _access=USER))
    assert [r["action"] for r in rows] == ["task.handoff"] * expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"success": True, "confirm": True}, 1),
        ({"success": True, "confirm": False}, 0),  # preview: nothing written
        ({"success": False, "confirm": True}, 0),  # rejected triage
    ],
)
def test_apply_correction_audits_only_a_confirmed_success(
    rows, monkeypatch, tmp_path, result, expected
):
    monkeypatch.setattr(execution, "_resolve_task", lambda _t: (None, None, tmp_path))

    async def _apply(*_a, **_k):
        return dict(result)

    monkeypatch.setattr(execution, "apply_correction", _apply)
    req = execution.ApplyCorrectionRequest(
        fix_request_md="# fix", source="tfactory", confirm=result["confirm"]
    )
    _run(execution.apply_task_correction("p:001", req, _access=USER))
    assert [r["action"] for r in rows] == ["task.apply_correction"] * expected
