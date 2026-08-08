#!/usr/bin/env python3
"""A plan approved against commit A must not build against commit B in silence.

Issue #1109. The signature gate answers "did anyone edit the instructions". It
cannot answer "are the instructions still about this codebase" -- and it
correctly still verifies for a plan that is byte-perfect and six weeks stale.
The contract carries the approved commit twice, inside the signed bytes, and
nothing read it back.

Every test here runs against a REAL git repo built in tmp_path: two commits, a
blast-radius file, and the actual `git rev-parse` / `rev-list` / `diff` the gate
shells out to. A mocked git would prove only that the mock was called.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from trusted_plan import (
    APPROVAL_KEY,
    DRIFT_ABSENT,
    DRIFT_BLAST_RADIUS,
    DRIFT_CURRENT,
    DRIFT_DRIFTED,
    DRIFT_UNKNOWN,
    baseline_drift_rejects,
    check_baseline_drift,
    drift_policy,
    sign_plan,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.routes import execution as execution_routes  # noqa: E402

KEY = "super-secret-cfactory-key"
PLANNED_FILE = "app/routers/status.py"
UNRELATED_FILE = "README.md"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/run/current-system/sw/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real two-file git repo at commit A."""
    project = tmp_path / "proj"
    (project / "app" / "routers").mkdir(parents=True)
    (project / ".aifactory" / "specs").mkdir(parents=True)
    (project / PLANNED_FILE).write_text("# v1\n")
    (project / UNRELATED_FILE).write_text("# docs v1\n")

    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "commit A")
    return project


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _advance(repo: Path, path: str, body: str) -> str:
    (repo / path).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"touch {path}")
    return _head(repo)


def _plan(baseline_commit: str | None, *, blast_radius=(PLANNED_FILE,)) -> dict:
    plan: dict = {
        "feature": "Signing key rotation status endpoint",
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
                        "files_to_create": [PLANNED_FILE],
                    }
                ],
            }
        ],
    }
    if baseline_commit is not None:
        plan["provenance"] = {
            "source": "pfactory",
            "plan_id": "028-signing-key-rotation-status-endpoint",
            "repo": "olafkfreund/PFactory",
            "baseline_commit": baseline_commit,
        }
        plan["baseline"] = {
            "repo": "olafkfreund/PFactory",
            "base_ref": "main",
            "commit": baseline_commit,
            "blast_radius": {"files": list(blast_radius)},
        }
    return plan


def _signed(plan: dict) -> dict:
    plan = json.loads(json.dumps(plan))
    plan[APPROVAL_KEY] = sign_plan(
        plan, key=KEY, approved_by="cfactory", approval_timestamp="2026-06-06T10:00:00Z"
    )
    return plan


# --------------------------------------------------------------------------
# The detector
# --------------------------------------------------------------------------


def test_head_equals_the_approved_commit_is_current(repo):
    drift = check_baseline_drift(_plan(_head(repo)), repo)
    assert drift.status == DRIFT_CURRENT
    assert drift.commits_ahead == 0
    assert not drift.diverged


def test_unrelated_commit_is_drift_but_not_a_blast_radius_change(repo):
    approved = _head(repo)
    _advance(repo, UNRELATED_FILE, "# docs v2\n")

    drift = check_baseline_drift(_plan(approved), repo)

    assert drift.status == DRIFT_DRIFTED
    assert drift.diverged
    assert drift.commits_ahead == 1
    assert drift.changed_blast_radius_files == []
    assert approved[:12] in drift.reason()
    assert _head(repo)[:12] in drift.reason()


def test_a_planned_file_changing_is_a_blast_radius_change(repo):
    approved = _head(repo)
    _advance(repo, PLANNED_FILE, "# v2 - someone else got here first\n")

    drift = check_baseline_drift(_plan(approved), repo)

    assert drift.status == DRIFT_BLAST_RADIUS
    assert drift.changed_blast_radius_files == [PLANNED_FILE]
    assert PLANNED_FILE in drift.reason()


def test_distance_counts_every_commit_since_approval(repo):
    approved = _head(repo)
    _advance(repo, UNRELATED_FILE, "# docs v2\n")
    _advance(repo, UNRELATED_FILE, "# docs v3\n")
    _advance(repo, UNRELATED_FILE, "# docs v4\n")

    drift = check_baseline_drift(_plan(approved), repo)
    assert drift.commits_ahead == 3
    assert "3 commit(s)" in drift.reason()


def test_no_baseline_in_the_contract_is_absent_not_drift(repo):
    """Greenfield / v1 plans carry no baseline. Silence, not a rejection."""
    drift = check_baseline_drift(_plan(None), repo)
    assert drift.status == DRIFT_ABSENT
    assert not drift.diverged
    assert not baseline_drift_rejects(drift, "reject")


def test_a_commit_this_clone_has_never_seen_is_unknown_not_drift(repo):
    """A shallow clone must not become an outage, and must not pass silently."""
    drift = check_baseline_drift(_plan("0" * 40), repo)
    assert drift.status == DRIFT_UNKNOWN
    assert not drift.diverged
    assert not baseline_drift_rejects(drift, "reject")
    assert "not present in this clone" in (drift.detail or "")


def test_not_a_git_repo_is_unknown(tmp_path):
    drift = check_baseline_drift(_plan("0" * 40), tmp_path)
    assert drift.status == DRIFT_UNKNOWN


def test_a_short_sha_is_not_drift(repo):
    """The approved commit abbreviated is the same commit."""
    drift = check_baseline_drift(_plan(_head(repo)[:10]), repo)
    assert drift.status == DRIFT_CURRENT


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


def test_default_policy_rejects_only_a_blast_radius_change(repo, monkeypatch):
    monkeypatch.delenv("AIFACTORY_BASELINE_DRIFT", raising=False)
    approved = _head(repo)

    _advance(repo, UNRELATED_FILE, "# docs v2\n")
    assert not baseline_drift_rejects(check_baseline_drift(_plan(approved), repo))

    _advance(repo, PLANNED_FILE, "# v2\n")
    assert baseline_drift_rejects(check_baseline_drift(_plan(approved), repo))


def test_reject_policy_rejects_any_divergence(repo, monkeypatch):
    monkeypatch.setenv("AIFACTORY_BASELINE_DRIFT", "reject")
    approved = _head(repo)
    _advance(repo, UNRELATED_FILE, "# docs v2\n")

    assert baseline_drift_rejects(check_baseline_drift(_plan(approved), repo))


def test_warn_and_off_never_reject(repo, monkeypatch):
    approved = _head(repo)
    _advance(repo, PLANNED_FILE, "# v2\n")
    drift = check_baseline_drift(_plan(approved), repo)

    for policy in ("warn", "off"):
        monkeypatch.setenv("AIFACTORY_BASELINE_DRIFT", policy)
        assert not baseline_drift_rejects(drift)


def test_a_typo_in_the_policy_env_does_not_disable_the_gate(monkeypatch):
    """A misspelt env var must not silently turn a control off (Factory#431)."""
    monkeypatch.setenv("AIFACTORY_BASELINE_DRIFT", "of")
    assert drift_policy() == "blast-radius"
    monkeypatch.setenv("AIFACTORY_BASELINE_DRIFT", "")
    assert drift_policy() == "blast-radius"


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


def _post(project_path: Path, plan: dict):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_service = MagicMock()
    fake_service.start_task_execution = AsyncMock(return_value=None)

    app = FastAPI()
    app.include_router(execution_routes.router, prefix="/api/tasks")

    with (
        patch.object(
            execution_routes,
            "load_projects",
            return_value={"p1": {"path": str(project_path)}},
        ),
        patch.object(execution_routes, "resolve_project_id", return_value="p1"),
        patch.object(execution_routes, "get_agent_service", return_value=fake_service),
        patch.object(execution_routes, "emit_task_status", AsyncMock()),
    ):
        client = TestClient(app)
        return client.post(
            "/api/tasks/from-plan",
            params={
                "project_id": "p1",
                "title": "Signing key rotation status endpoint",
                "description": "signed contract",
            },
            json={"plan": plan},
        )


def test_endpoint_rejects_a_stale_plan_naming_both_commits(repo, monkeypatch):
    """The beat of the demo that could not be filmed: there was no output."""
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", KEY)
    approved = _head(repo)
    head = _advance(repo, PLANNED_FILE, "# v2 - someone else got here first\n")

    resp = _post(repo, _signed(_plan(approved)))

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "Plan rejected — approved against a stale baseline"
    reason = detail["reasons"][0]
    assert approved[:12] in reason and head[:12] in reason
    assert PLANNED_FILE in reason
    assert detail["baseline_drift"]["status"] == DRIFT_BLAST_RADIUS


def test_a_stale_rejection_allocates_nothing(repo, monkeypatch):
    """Gate first, allocate second (#1108) -- the new gate obeys the same rule."""
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", KEY)
    approved = _head(repo)
    _advance(repo, PLANNED_FILE, "# v2\n")
    specs_dir = repo / ".aifactory" / "specs"

    assert _post(repo, _signed(_plan(approved))).status_code == 422

    assert [d.name for d in specs_dir.iterdir() if d.is_dir()] == []
    assert not (specs_dir / ".counter").exists()


def test_a_current_plan_still_builds(repo, monkeypatch):
    """Mutation check: the gate must not reject a plan approved against HEAD."""
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", KEY)

    resp = _post(repo, _signed(_plan(_head(repo))))

    assert resp.status_code == 200, resp.text
    spec_id = resp.json()["task_id"].split(":", 1)[1]
    prov = json.loads(
        (repo / ".aifactory" / "specs" / spec_id / "requirements.json").read_text()
    )["provenance"]
    assert prov["baseline_drift"]["status"] == DRIFT_CURRENT


def test_unrelated_drift_builds_but_is_recorded_on_the_provenance(repo, monkeypatch):
    """ "We proceeded" is a decision, and it has to be auditable on the spec."""
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", KEY)
    approved = _head(repo)
    head = _advance(repo, UNRELATED_FILE, "# docs v2\n")

    resp = _post(repo, _signed(_plan(approved)))

    assert resp.status_code == 200, resp.text
    spec_id = resp.json()["task_id"].split(":", 1)[1]
    prov = json.loads(
        (repo / ".aifactory" / "specs" / spec_id / "requirements.json").read_text()
    )["provenance"]
    assert prov["baseline_drift"] == {
        "status": DRIFT_DRIFTED,
        "baseline_commit": approved,
        "head_commit": head,
        "commits_ahead": 1,
    }


def test_a_plan_with_no_baseline_records_nothing(repo, monkeypatch):
    """Greenfield plans keep their previous provenance exactly."""
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", KEY)

    resp = _post(repo, _signed(_plan(None)))

    assert resp.status_code == 200, resp.text
    spec_id = resp.json()["task_id"].split(":", 1)[1]
    prov = json.loads(
        (repo / ".aifactory" / "specs" / spec_id / "requirements.json").read_text()
    ).get("provenance", {})
    assert "baseline_drift" not in prov


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
