#!/usr/bin/env python3
"""Unit tests for V1-B (#94) — Copilot delegation in Auto-Fix.

Covers:
- delegation_formatter.render_plan_as_comment renders all 4 sections
  (Spec / Acceptance / Affected files / Implementation plan) and the
  Copilot mention footer.
- delegation_tracker.scan_delegated_tasks promotes a delegated item to
  in_review when a matching Copilot PR is found, and declines it
  after 24h with no PR.
- start_auto_fix takes the delegation branch when enableDelegation is
  set on a GitHub project, and the default branch otherwise.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from server.services.delegation_formatter import render_plan_as_comment  # noqa: E402
from server.services.delegation_tracker import (  # noqa: E402
    DECLINE_AFTER_HOURS,
    scan_delegated_tasks,
)

# ---------------------------------------------------------------------------
# delegation_formatter
# ---------------------------------------------------------------------------


def test_formatter_renders_all_sections(tmp_path: Path):
    spec_md = tmp_path / "spec.md"
    spec_md.write_text(
        "# Add user logout endpoint\n\n## Description\n\nReturn 204 on POST /logout."
    )
    plan_json = tmp_path / "implementation_plan.json"
    plan_json.write_text(
        json.dumps(
            {
                "acceptance_criteria": [
                    "POST /logout returns 204",
                    "Session cookie is cleared",
                ],
                "phases": [
                    {
                        "name": "Backend",
                        "subtasks": [
                            {
                                "description": "Add /logout route handler",
                                "affected_files": ["src/auth/routes.py"],
                            }
                        ],
                    },
                    {
                        "name": "Tests",
                        "subtasks": [
                            {
                                "description": "Cover logout happy path",
                                "affected_files": ["tests/test_auth.py"],
                            }
                        ],
                    },
                ],
            }
        )
    )

    comment = render_plan_as_comment(plan_json, spec_md)

    assert "## ✨ AIFactory enrichment" in comment
    assert "### Spec" in comment
    assert "Return 204 on POST /logout." in comment
    # The first H1 is stripped to avoid duplicating the issue title.
    assert "# Add user logout endpoint" not in comment
    assert "### Acceptance criteria" in comment
    assert "- [ ] POST /logout returns 204" in comment
    assert "### Affected files" in comment
    assert "- `src/auth/routes.py`" in comment
    assert "- `tests/test_auth.py`" in comment
    assert "### Implementation plan" in comment
    assert "**Backend**" in comment
    assert "**Tests**" in comment
    assert "@Copilot — please implement." in comment


def test_formatter_handles_missing_files(tmp_path: Path):
    # Both inputs missing — should produce a usable comment, not raise.
    comment = render_plan_as_comment(
        tmp_path / "missing_plan.json",
        tmp_path / "missing_spec.md",
    )
    assert "## ✨ AIFactory enrichment" in comment
    assert "_Spec body unavailable._" in comment
    assert "_No explicit acceptance criteria listed in the plan._" in comment
    assert "_No affected files listed in the plan._" in comment
    assert "_Plan structure unavailable._" in comment


def test_formatter_dedupes_files_across_subtasks(tmp_path: Path):
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(
        json.dumps(
            {
                "subtasks": [
                    {"description": "A", "affected_files": ["x.py", "y.py"]},
                    {"description": "B", "affected_files": ["x.py", "z.py"]},
                ]
            }
        )
    )
    comment = render_plan_as_comment(plan_json, tmp_path / "missing.md")
    # Each file appears exactly once even though x.py is in both subtasks.
    assert comment.count("`x.py`") == 1
    assert comment.count("`y.py`") == 1
    assert comment.count("`z.py`") == 1


# ---------------------------------------------------------------------------
# delegation_tracker
# ---------------------------------------------------------------------------


def _stub_pr(number: int, author: str, title: str, body: str = "", url: str = ""):
    pr = MagicMock()
    pr.number = number
    pr.author = author
    pr.title = title
    pr.body = body
    pr.url = url or f"https://example.test/pr/{number}"
    return pr


@pytest.mark.asyncio
async def test_tracker_promotes_to_in_review_when_copilot_pr_matches():
    delegated_item = {
        "issueNumber": 42,
        "specId": "001-gh42-test",
        "status": "delegated",
        "delegatedAt": datetime.now(UTC).isoformat(),
    }
    mock_provider = MagicMock()
    mock_provider.provider_type = "github"
    mock_provider.fetch_prs = AsyncMock(
        return_value=[
            _stub_pr(
                number=501,
                author="copilot-swe-agent",
                title="Fix #42: add logout",
                body="Closes #42",
                url="https://example.test/pr/501",
            )
        ]
    )
    upserted: list[dict] = []
    emitted: list[tuple] = []

    with (
        patch(
            "server.services.auto_fix_service.get_queue", return_value=[delegated_item]
        ),
        patch(
            "server.services.auto_fix_service._provider_for", return_value=mock_provider
        ),
        patch(
            "server.services.auto_fix_service._upsert_queue_item",
            side_effect=lambda pid, item: upserted.append(item),
        ),
        patch(
            "server.websockets.events.emit_task_status",
            new=AsyncMock(side_effect=lambda *args, **kw: emitted.append((args, kw))),
        ),
    ):
        summary = await scan_delegated_tasks("proj-1")

    assert summary["promoted"] == [
        {"issueNumber": 42, "prNumber": 501, "prUrl": "https://example.test/pr/501"}
    ]
    assert summary["declined"] == []
    assert upserted and upserted[0]["status"] == "in_review"
    assert upserted[0]["prNumber"] == 501


@pytest.mark.asyncio
async def test_tracker_declines_after_24h_with_no_match():
    too_old = datetime.now(UTC) - timedelta(hours=DECLINE_AFTER_HOURS + 1)
    delegated_item = {
        "issueNumber": 42,
        "specId": "001-gh42-test",
        "status": "delegated",
        "delegatedAt": too_old.isoformat(),
    }
    mock_provider = MagicMock()
    mock_provider.provider_type = "github"
    mock_provider.fetch_prs = AsyncMock(return_value=[])
    upserted: list[dict] = []

    with (
        patch(
            "server.services.auto_fix_service.get_queue", return_value=[delegated_item]
        ),
        patch(
            "server.services.auto_fix_service._provider_for", return_value=mock_provider
        ),
        patch(
            "server.services.auto_fix_service._upsert_queue_item",
            side_effect=lambda pid, item: upserted.append(item),
        ),
        patch("server.websockets.events.emit_task_status", new=AsyncMock()),
    ):
        summary = await scan_delegated_tasks("proj-1")

    assert summary["promoted"] == []
    assert summary["declined"] == [{"issueNumber": 42}]
    assert upserted and upserted[0]["status"] == "declined"


@pytest.mark.asyncio
async def test_tracker_keeps_pending_within_window():
    just_now = datetime.now(UTC).isoformat()
    delegated_item = {
        "issueNumber": 42,
        "specId": "001-gh42-test",
        "status": "delegated",
        "delegatedAt": just_now,
    }
    mock_provider = MagicMock()
    mock_provider.provider_type = "github"
    mock_provider.fetch_prs = AsyncMock(return_value=[])
    upserted: list[dict] = []

    with (
        patch(
            "server.services.auto_fix_service.get_queue", return_value=[delegated_item]
        ),
        patch(
            "server.services.auto_fix_service._provider_for", return_value=mock_provider
        ),
        patch(
            "server.services.auto_fix_service._upsert_queue_item",
            side_effect=lambda pid, item: upserted.append(item),
        ),
        patch("server.websockets.events.emit_task_status", new=AsyncMock()),
    ):
        summary = await scan_delegated_tasks("proj-1")

    assert summary["promoted"] == []
    assert summary["declined"] == []
    assert upserted == []  # No transition while within the decline window


# ---------------------------------------------------------------------------
# start_auto_fix delegation branch
# ---------------------------------------------------------------------------


def _make_project_layout(tmp_path: Path, enable_delegation: bool) -> Path:
    """Build a minimal on-disk project that auto_fix_service can read.

    Returns the project root path.
    """
    project_path = tmp_path / "proj"
    spec_dir = project_path / ".aifactory" / "specs" / "001-gh42-test"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.json").write_text(
        json.dumps(
            {
                "title": "Test issue",
                "description": "Body",
                "metadata": {"enableDelegation": enable_delegation},
            }
        )
    )
    (spec_dir / "spec.md").write_text("# Test issue\n\nBody")
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"acceptance_criteria": ["it works"], "subtasks": []})
    )
    return project_path


@pytest.mark.asyncio
async def test_start_auto_fix_delegation_branch_assigns_copilot(tmp_path: Path):
    """When enableDelegation=True + provider=github, the delegation
    branch fires: status="delegated", comment posted, Copilot assigned,
    coder/QA pipeline skipped."""
    project_path = _make_project_layout(tmp_path, enable_delegation=True)

    projects_fixture = {
        "proj-1": {
            "path": str(project_path),
            "settings": {"gitProvider": "github", "gitRepo": "acme/widgets"},
        }
    }

    mock_provider = MagicMock()
    mock_provider.add_comment = AsyncMock(return_value=1)
    mock_provider.assign_to_user = AsyncMock(return_value=None)

    mock_agent_service = MagicMock()
    mock_agent_service.start_task_execution = AsyncMock(return_value=None)

    with (
        patch("server.routes.projects.load_projects", return_value=projects_fixture),
        patch(
            "server.services.auto_fix_service._provider_for", return_value=mock_provider
        ),
        patch(
            "server.services.agent_service.get_agent_service",
            return_value=mock_agent_service,
        ),
        patch("server.services.auto_fix_service._upsert_queue_item"),
        patch("server.websockets.events.broadcast_event", new=AsyncMock()),
        patch("server.websockets.events.emit_task_status", new=AsyncMock()),
    ):
        from server.services.auto_fix_service import start_auto_fix

        result = await start_auto_fix("proj-1", 42)

    assert result["status"] == "delegated"
    # Planner-only invocation
    mock_agent_service.start_task_execution.assert_awaited_once()
    kwargs = mock_agent_service.start_task_execution.await_args.kwargs
    assert kwargs.get("stop_after_planning") is True
    # Copilot assignment
    mock_provider.assign_to_user.assert_awaited_once()
    assignees = mock_provider.assign_to_user.await_args.args[1]
    assert "Copilot" in assignees
    # Enriched comment was posted
    mock_provider.add_comment.assert_awaited_once()
    posted_body = mock_provider.add_comment.await_args.args[1]
    assert "## ✨ AIFactory enrichment" in posted_body


@pytest.mark.asyncio
async def test_start_auto_fix_default_branch_when_delegation_off(tmp_path: Path):
    """Regression: when enableDelegation is not set, the full pipeline runs
    as before (no comment, no Copilot assignment)."""
    project_path = _make_project_layout(tmp_path, enable_delegation=False)

    projects_fixture = {
        "proj-1": {
            "path": str(project_path),
            "settings": {"gitProvider": "github", "gitRepo": "acme/widgets"},
        }
    }

    mock_provider = MagicMock()
    mock_provider.add_comment = AsyncMock(return_value=1)
    mock_provider.assign_to_user = AsyncMock(return_value=None)

    mock_agent_service = MagicMock()
    mock_agent_service.start_task_execution = AsyncMock(return_value=None)

    with (
        patch("server.routes.projects.load_projects", return_value=projects_fixture),
        patch(
            "server.services.auto_fix_service._provider_for", return_value=mock_provider
        ),
        patch(
            "server.services.agent_service.get_agent_service",
            return_value=mock_agent_service,
        ),
        patch("server.services.auto_fix_service._upsert_queue_item"),
        patch("server.websockets.events.broadcast_event", new=AsyncMock()),
    ):
        from server.services.auto_fix_service import start_auto_fix

        result = await start_auto_fix("proj-1", 42)

    assert result["status"] == "started"
    # Full pipeline — no stop_after_planning kwarg
    mock_agent_service.start_task_execution.assert_awaited_once()
    kwargs = mock_agent_service.start_task_execution.await_args.kwargs
    assert kwargs.get("stop_after_planning") in (None, False)
    # No comment, no Copilot assignment in the non-delegation branch
    mock_provider.add_comment.assert_not_awaited()
    mock_provider.assign_to_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_auto_fix_delegation_skipped_for_ado_provider(
    tmp_path: Path,
):
    """Azure DevOps has no autonomous coding agent. Even with
    enableDelegation=True, ADO projects must fall through to the local
    pipeline. GitHub + GitLab both take the delegation branch
    (V1 + V1.5)."""
    project_path = _make_project_layout(tmp_path, enable_delegation=True)
    # Rename the spec dir so the ado provider_type ("wi" prefix) finds it.
    src = project_path / ".aifactory" / "specs" / "001-gh42-test"
    dst = project_path / ".aifactory" / "specs" / "001-wi42-test"
    src.rename(dst)

    projects_fixture = {
        "proj-1": {
            "path": str(project_path),
            "settings": {
                "gitProvider": "azure_devops",
                "gitRepo": "acme/widgets",
            },
        }
    }

    mock_provider = MagicMock()
    mock_provider.add_comment = AsyncMock(return_value=1)
    mock_provider.assign_to_user = AsyncMock(return_value=None)

    mock_agent_service = MagicMock()
    mock_agent_service.start_task_execution = AsyncMock(return_value=None)

    with (
        patch("server.routes.projects.load_projects", return_value=projects_fixture),
        patch(
            "server.services.auto_fix_service._provider_for", return_value=mock_provider
        ),
        patch(
            "server.services.agent_service.get_agent_service",
            return_value=mock_agent_service,
        ),
        patch("server.services.auto_fix_service._upsert_queue_item"),
        patch("server.websockets.events.broadcast_event", new=AsyncMock()),
    ):
        from server.services.auto_fix_service import start_auto_fix

        result = await start_auto_fix("proj-1", 42)

    assert result["status"] == "started"
    mock_provider.assign_to_user.assert_not_awaited()
