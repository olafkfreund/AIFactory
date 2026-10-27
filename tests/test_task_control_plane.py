#!/usr/bin/env python3
"""Regression tests for control-plane state isolation (Issue #259).

The bug class: control-plane state (board column / task status /
``reviewReason``) used to live inside ``implementation_plan.json`` — the
same file an agent rewrites in its isolated worktree. A worktree sync
therefore risked clobbering a human/system review decision.

These tests pin the fix: control-plane state now lives in a dedicated,
agent-immutable ``task_control.json`` store, and the worktree sync path
strips control fields so it can NEVER reset that state.

The headline regression test (``test_agent_sync_does_not_reset_human_review``)
reproduces the original bug end-to-end:
  1. a human sets a review status (human_review / qa_rejected),
  2. the agent emits a fresh implementation_plan.json into its worktree
     (with NO review status, mimicking the agent's view of the world),
  3. a ``_sync_worktree_files`` tick runs,
  4. we assert the control-plane status/reviewReason is NOT reset.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Make the web-server package importable (mirrors test_mcp_status_route.py).
_WEB_SERVER_DIR = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

try:
    from server.services import task_control
    from server.services.agent_service import AgentService

    _IMPORT_OK = True
except Exception:  # pragma: no cover - skip when web-server deps unavailable
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK,
    reason="web-server package/deps not importable",
)


def _make_plan(subtask_statuses: dict, **top_level) -> dict:
    plan = {
        "phases": [
            {
                "id": "phase-1",
                "name": "Implementation",
                "subtasks": [
                    {"id": sid, "description": f"Subtask {sid}", "status": st}
                    for sid, st in subtask_statuses.items()
                ],
            }
        ]
    }
    plan.update(top_level)
    return plan


# ---------------------------------------------------------------------------
# task_control store unit behaviour
# ---------------------------------------------------------------------------


class TestTaskControlStore:
    def test_write_then_read_roundtrip(self, tmp_path):
        spec = tmp_path / "001-feature"
        task_control.write_control(
            spec, status="human_review", review_reason="plan_review"
        )
        got = task_control.read_control(spec)
        assert got["status"] == "human_review"
        assert got["reviewReason"] == "plan_review"
        assert got["updatedBy"] == "web_server"
        assert "updatedAt" in got

    def test_atomic_write_uses_replace(self, tmp_path):
        """No stray .tmp files should remain after a write."""
        spec = tmp_path / "001-feature"
        task_control.write_control(spec, status="in_progress")
        leftovers = [p.name for p in spec.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []
        assert (spec / task_control.CONTROL_FILE_NAME).exists()

    def test_partial_update_merges(self, tmp_path):
        spec = tmp_path / "001-feature"
        task_control.write_control(
            spec, status="human_review", review_reason="qa_rejected"
        )
        # Status-only update keeps the existing reviewReason.
        task_control.write_control(spec, status="human_review")
        got = task_control.read_control(spec)
        assert got["reviewReason"] == "qa_rejected"

    def test_clear_review_reason(self, tmp_path):
        spec = tmp_path / "001-feature"
        task_control.write_control(
            spec, status="human_review", review_reason="plan_review"
        )
        task_control.write_control(spec, status="in_progress", clear_review_reason=True)
        got = task_control.read_control(spec)
        assert got["status"] == "in_progress"
        assert "reviewReason" not in got

    def test_read_time_migration_from_plan(self, tmp_path):
        """Pre-#259 specs (status only in plan file) still surface their state."""
        spec = tmp_path / "001-feature"
        spec.mkdir()
        plan = _make_plan(
            {"st-1": "completed"}, status="human_review", reviewReason="completed"
        )
        (spec / "implementation_plan.json").write_text(json.dumps(plan))
        got = task_control.read_control(spec)
        assert got["status"] == "human_review"
        assert got["reviewReason"] == "completed"
        assert got.get("_migratedFromPlan") is True

    def test_strip_control_fields(self):
        plan = _make_plan(
            {"st-1": "completed"}, status="human_review", reviewReason="x"
        )
        task_control.strip_control_fields(plan)
        assert "status" not in plan
        assert "reviewReason" not in plan
        # Agent artifact data is untouched.
        assert plan["phases"][0]["subtasks"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# The headline regression: worktree sync must not reset control-plane state
# ---------------------------------------------------------------------------


class TestSyncDoesNotClobberControlPlane:
    def _build_spec_layout(self, project_path: Path, spec_id: str):
        """Create main + worktree spec dirs the way the app lays them out."""
        main_spec = project_path / ".aifactory" / "specs" / spec_id
        worktree_spec = (
            project_path
            / ".aifactory"
            / "worktrees"
            / "tasks"
            / spec_id
            / ".aifactory"
            / "specs"
            / spec_id
        )
        main_spec.mkdir(parents=True)
        worktree_spec.mkdir(parents=True)
        return main_spec, worktree_spec

    def test_agent_sync_does_not_reset_human_review(self, tmp_path):
        """Reproduce Issue #259: human sets review status, agent syncs, status survives."""
        project_path = tmp_path / "proj"
        spec_id = "001-feature"
        main_spec, worktree_spec = self._build_spec_layout(project_path, spec_id)

        # 1) Main spec has the agent's plan (NO control fields — they live in the
        #    dedicated store now) plus the human's review decision.
        main_plan = _make_plan({"st-1": "completed", "st-2": "in_progress"})
        (main_spec / "implementation_plan.json").write_text(json.dumps(main_plan))

        # Human sets a review status via the control store (e.g. kanban move /
        # QA rejection).
        task_control.write_control(
            main_spec,
            status="human_review",
            review_reason="qa_rejected",
            updated_by="web_user",
        )

        # 2) Agent emits a fresh plan into its worktree. Crucially this mimics
        #    the original bug trigger: the agent's plan carries a STALE/foreign
        #    top-level status that, pre-fix, would land in the main plan file.
        worktree_plan = _make_plan(
            {"st-1": "completed", "st-2": "completed"},
            status="in_progress",  # agent's view — must NOT win
            reviewReason="completed",  # agent's view — must NOT win
        )
        (worktree_spec / "implementation_plan.json").write_text(
            json.dumps(worktree_plan)
        )

        # 3) A _sync_worktree_files tick runs (the exact path from the bug report).
        service = AgentService()
        asyncio.run(
            service._sync_worktree_files(
                project_path, spec_id, task_id=f"proj:{spec_id}"
            )
        )

        # 4a) Control-plane state is intact in the dedicated store.
        control = task_control.read_control(main_spec)
        assert control["status"] == "human_review", (
            "human review status was reset by agent sync"
        )
        assert control["reviewReason"] == "qa_rejected", (
            "reviewReason was reset by agent sync"
        )

        # 4b) The synced plan file must NOT carry the agent's control fields —
        #     otherwise a reader could pick them over the control store.
        synced_plan = json.loads((main_spec / "implementation_plan.json").read_text())
        assert "status" not in synced_plan, "agent control status leaked into main plan"
        assert "reviewReason" not in synced_plan, (
            "agent reviewReason leaked into main plan"
        )

        # 4c) Forward subtask progress from the worktree still syncs (artifact data).
        synced_subtasks = {
            s["id"]: s["status"] for p in synced_plan["phases"] for s in p["subtasks"]
        }
        assert synced_subtasks["st-2"] == "completed", (
            "forward subtask progress was lost"
        )

    def test_sync_falls_back_safely_on_corrupt_worktree_plan(self, tmp_path):
        """A corrupt worktree plan must not crash sync nor reset control state."""
        project_path = tmp_path / "proj"
        spec_id = "002-feature"
        main_spec, worktree_spec = self._build_spec_layout(project_path, spec_id)

        (main_spec / "implementation_plan.json").write_text(
            json.dumps(_make_plan({"st-1": "completed"}))
        )
        task_control.write_control(
            main_spec, status="human_review", review_reason="completed"
        )

        # Corrupt JSON in the worktree triggers the except branch.
        (worktree_spec / "implementation_plan.json").write_text("{ not valid json")

        service = AgentService()
        asyncio.run(
            service._sync_worktree_files(
                project_path, spec_id, task_id=f"proj:{spec_id}"
            )
        )

        control = task_control.read_control(main_spec)
        assert control["status"] == "human_review"
        assert control["reviewReason"] == "completed"


def test_agent_sync_does_not_overwrite_requirements(tmp_path, monkeypatch):
    """Factory#1960: an agent edit to the worktree requirements.json must not
    reach the canonical copy the commit trailers read."""
    from core.commit_trailers import trailer_block

    monkeypatch.setenv("AIFACTORY_AGENT_COMMIT_TRAILERS", "1")
    project_path = tmp_path / "proj"
    spec_id = "001-feature"
    main_spec, worktree_spec = TestSyncDoesNotClobberControlPlane()._build_spec_layout(
        project_path, spec_id
    )
    canonical = {
        "agent_identity": "agent:aifactory/k1",
        "intake": {"autonomy_tier": "low"},
        "originator": "human:github/someone",
    }
    (main_spec / "requirements.json").write_text(json.dumps(canonical))
    before = trailer_block(main_spec)
    forged = {
        "agent_identity": "human:github/someone",
        "intake": {"autonomy_tier": "hard"},
        "originator": "human:github/admin",
    }
    (worktree_spec / "requirements.json").write_text(json.dumps(forged))

    asyncio.run(
        AgentService()._sync_worktree_files(
            project_path, spec_id, task_id=f"proj:{spec_id}"
        )
    )

    assert json.loads((main_spec / "requirements.json").read_text()) == canonical
    assert trailer_block(main_spec) == before
    assert "Agent-Identity: agent:aifactory/k1" in before


def test_agent_sync_does_not_copy_control_files(tmp_path):
    """The extra-files loop must not mirror the agent's control-store copies."""
    project_path = tmp_path / "proj"
    spec_id = "001-feature"
    main_spec, worktree_spec = TestSyncDoesNotClobberControlPlane()._build_spec_layout(
        project_path, spec_id
    )
    for name in ("task_control.json", "qa_review_cycle.json"):
        (main_spec / name).write_text('{"owner": "control-plane"}')
        (worktree_spec / name).write_text('{"owner": "agent"}')

    asyncio.run(
        AgentService()._sync_worktree_files(
            project_path, spec_id, task_id=f"proj:{spec_id}"
        )
    )

    for name in ("task_control.json", "qa_review_cycle.json"):
        assert (main_spec / name).read_text() == '{"owner": "control-plane"}'


def _run_sync(project_path, spec_id):
    asyncio.run(
        AgentService()._sync_worktree_files(
            project_path, spec_id, task_id=f"proj:{spec_id}"
        )
    )


def test_agent_sync_does_not_overwrite_canonical_control_files(tmp_path):
    """#1921: the extra-files loop copied every agent-writable top-level file.

    Forged task_metadata.json (merge tier, tenant, budget, base branch),
    review_state.json (plan approval) and COMPLETED.json in the worktree must
    leave the canonical copies, and what the control plane reads off them,
    unchanged. So must a file nobody allowlisted.
    """
    from server.services.pr_endgame import review_tier_of
    from server.tenancy import spec_tenant

    project_path = tmp_path / "proj"
    spec_id = "001-feature"
    main_spec, worktree_spec = TestSyncDoesNotClobberControlPlane()._build_spec_layout(
        project_path, spec_id
    )
    canonical = {
        "task_metadata.json": json.dumps(
            {
                "reviewTier": "blocking",
                "tenant_id": "tenant-a",
                "budgetUsd": 5,
                "base_branch": "dev",
            }
        ),
        "review_state.json": json.dumps({"approved": False}),
        "COMPLETED.json": json.dumps({"status": "done", "run": 1}),
    }
    forged = {
        "task_metadata.json": json.dumps(
            {"reviewTier": "low", "tenant_id": "tenant-b", "base_branch": "main"}
        ),
        "review_state.json": json.dumps({"approved": True}),
        "COMPLETED.json": json.dumps({"status": "done", "run": 0}),
    }
    for name, text in canonical.items():
        (main_spec / name).write_text(text)
        (worktree_spec / name).write_text(forged[name])
    (worktree_spec / "agent_notes.md").write_text("not allowlisted")

    _run_sync(project_path, spec_id)

    for name, text in canonical.items():
        assert (main_spec / name).read_text() == text, name
    assert not (main_spec / "agent_notes.md").exists()
    meta = json.loads((main_spec / "task_metadata.json").read_text())
    assert review_tier_of(meta) == "blocking"
    assert spec_tenant(main_spec) == "tenant-a"


@pytest.mark.parametrize(
    "name",
    [
        "token_usage.json",
        "injection_scan.json",
        "guardrail_halt.json",
        "handback_received.json",
    ],
)
def test_agent_sync_still_mirrors_allowlisted_build_files(tmp_path, name):
    """#1921: build-written files the control plane reads still reach main."""
    project_path = tmp_path / "proj"
    spec_id = "001-feature"
    main_spec, worktree_spec = TestSyncDoesNotClobberControlPlane()._build_spec_layout(
        project_path, spec_id
    )
    (worktree_spec / name).write_text('{"from": "build"}')

    _run_sync(project_path, spec_id)

    assert (main_spec / name).read_text() == '{"from": "build"}'
