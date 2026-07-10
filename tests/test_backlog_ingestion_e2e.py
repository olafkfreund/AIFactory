#!/usr/bin/env python3
"""
E2E test: GitHub issue → Backlog ingestion + auto/manual start (delegation) (#395)
=================================================================================

Exercises the real auto-fix ingestion path with the GitHub boundary and the
agent/delegation runners mocked:

- check_new_issues lists open issues not yet imported (and excludes imported ones).
- start_auto_fix imports an issue → writes a spec (lands as a backlog task) →
  starts the agent (auto/manual start).
- Delegation mode (enableDelegation + GitHub) hands off to run_delegation and
  does NOT run the local coder pipeline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services import auto_fix_service as afs  # noqa: E402


def _issue(number, title="Add health endpoint", body="do it", labels=None):
    return SimpleNamespace(
        number=number,
        title=title,
        body=body,
        state="open",
        labels=labels or [],
        url=f"https://github.com/o/r/issues/{number}",
    )


class _FakeProvider:
    def __init__(self, issues):
        self._issues = {i.number: i for i in issues}

    async def fetch_issues(self, filters):
        return list(self._issues.values())

    async def fetch_issue(self, number):
        return self._issues[number]


class _StubAgent:
    def __init__(self):
        self.started = []

    async def start_task_execution(self, **kw):
        self.started.append(kw)


def _setup(tmp_path, monkeypatch, *, issues, settings=None):
    project = tmp_path / "proj"
    (project / ".aifactory" / "specs").mkdir(parents=True)
    projects = {
        "p": {"path": str(project), "settings": settings or {"gitProvider": "github"}}
    }

    import server.routes.projects as projects_mod
    import server.services.agent_service as agent_mod
    import server.websockets.events as events_mod

    monkeypatch.setattr(projects_mod, "load_projects", lambda: projects)
    monkeypatch.setattr(afs, "get_config", lambda pid: {"queue": [], "labels": []})
    monkeypatch.setattr(afs, "_provider_for", lambda pid: _FakeProvider(issues))
    monkeypatch.setattr(afs, "_upsert_queue_item", lambda pid, item: None)

    stub = _StubAgent()
    monkeypatch.setattr(agent_mod, "get_agent_service", lambda: stub)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(events_mod, "broadcast_event", _noop_broadcast)
    return project, stub


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


async def test_check_new_issues_lists_unimported(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, issues=[_issue(1), _issue(2)])
    new = await afs.check_new_issues("p")
    assert {i["number"] for i in new} == {1, 2}
    assert all(i["provider"] == "github" for i in new)


async def test_check_new_issues_excludes_already_imported(tmp_path, monkeypatch):
    project, _ = _setup(tmp_path, monkeypatch, issues=[_issue(5), _issue(6)])
    # Issue 5 already imported (spec dir carries the -gh5- marker).
    (project / ".aifactory" / "specs" / "001-gh5-existing").mkdir(parents=True)
    new = await afs.check_new_issues("p")
    assert {i["number"] for i in new} == {6}


# --------------------------------------------------------------------------
# Auto/manual start (default flow)
# --------------------------------------------------------------------------


async def test_start_auto_fix_imports_and_starts(tmp_path, monkeypatch):
    project, stub = _setup(tmp_path, monkeypatch, issues=[_issue(7)])
    result = await afs.start_auto_fix("p", 7)

    assert result["status"] == "started"
    # A spec was created and lands as a backlog task (gh7 marker).
    specs = [d.name for d in (project / ".aifactory" / "specs").iterdir()]
    assert any("-gh7-" in s for s in specs)
    # The agent was actually started for it.
    assert len(stub.started) == 1
    assert stub.started[0]["spec_id"] == result["specId"]


async def test_start_auto_fix_idempotent_reuses_spec(tmp_path, monkeypatch):
    project, stub = _setup(tmp_path, monkeypatch, issues=[_issue(8)])
    r1 = await afs.start_auto_fix("p", 8)
    r2 = await afs.start_auto_fix("p", 8)
    assert r1["specId"] == r2["specId"]  # reused, not duplicated
    specs = [
        d.name
        for d in (project / ".aifactory" / "specs").iterdir()
        if "-gh8-" in d.name
    ]
    assert len(specs) == 1


# --------------------------------------------------------------------------
# Delegation mode
# --------------------------------------------------------------------------


async def test_start_auto_fix_delegates_when_enabled(tmp_path, monkeypatch):
    project, stub = _setup(tmp_path, monkeypatch, issues=[_issue(9)])
    # Pre-create the spec with enableDelegation so ingestion takes the delegation branch.
    spec = project / ".aifactory" / "specs" / "001-gh9-deleg"
    spec.mkdir(parents=True)
    (spec / "requirements.json").write_text(
        json.dumps(
            {"title": "x", "description": "y", "metadata": {"enableDelegation": True}}
        )
    )

    import server.services.delegation_runner as dr

    calls = []

    async def _fake_delegation(**kw):
        calls.append(kw)
        return {"delegatedAt": "2026-06-06T12:00:00Z"}

    monkeypatch.setattr(dr, "run_delegation", _fake_delegation)

    result = await afs.start_auto_fix("p", 9)

    assert result["status"] == "delegated"
    assert len(calls) == 1  # handed off to the delegation runner
    assert stub.started == []  # local coder pipeline did NOT run


async def test_no_delegation_for_non_github(tmp_path, monkeypatch):
    # Same enableDelegation flag, but a non-GitHub/GitLab provider → no delegation.
    project, stub = _setup(
        tmp_path,
        monkeypatch,
        issues=[_issue(10)],
        settings={"gitProvider": "azure_devops"},
    )
    spec = project / ".aifactory" / "specs" / "001-wi10-x"
    spec.mkdir(parents=True)
    (spec / "requirements.json").write_text(
        json.dumps(
            {"title": "x", "description": "y", "metadata": {"enableDelegation": True}}
        )
    )
    result = await afs.start_auto_fix("p", 10)
    assert result["status"] == "started"
    assert len(stub.started) == 1  # ran locally, no delegation path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
