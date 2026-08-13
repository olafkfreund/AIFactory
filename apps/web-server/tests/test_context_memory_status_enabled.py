"""`memoryStatus.enabled` must reflect the project, not a constant (#1210).

The route computed the project's ``GRAPHITI_ENABLED`` and threw it away, then
reported ``enabled: True`` for every project regardless of its own config. A
field that reports the same value whatever the state is not a status — it is a
constant wearing a status's name, and the next consumer to reach for it gets a
confident wrong answer.

Wired rather than deleted: nothing reads it today, but it is in the published
OpenAPI and the TS interface, so removing it is a contract break for consumers
this repo cannot see. Making it true costs the same.

Both directions asserted. Only checking the ``false`` case would pass against a
field hardcoded to ``False``, which is the same bug mirrored.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server import project_store
from server.routes import context


def _project(tmp_path: Path, *, graphiti: str | None) -> Path:
    """A project whose .aifactory/.env either sets GRAPHITI_ENABLED or does not."""
    root = tmp_path / "proj"
    (root / ".aifactory" / "specs").mkdir(parents=True)
    if graphiti is not None:
        (root / ".aifactory" / ".env").write_text(f"GRAPHITI_ENABLED={graphiti}\n")
    return root


def _memory_status(tmp_path: Path, project_path: Path) -> dict[str, Any]:
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps({"p1": {"path": str(project_path), "name": "p1"}})
    )
    # The handler imports `load_projects` from `.projects` at call time, so the
    # patch has to land on the SOURCE module — and pointing at the real projects
    # file rather than stubbing the loader keeps the production read path.
    # Patch the owner (server/project_store.py, #1302), not the routes module
    # that merely re-exports it.
    with patch.object(project_store, "get_projects_file", return_value=projects_file):
        result = asyncio.run(context.get_project_context("p1"))
    status: dict[str, Any] = result["data"]["memoryStatus"]
    return status


def test_a_project_with_graphiti_off_reports_enabled_false(tmp_path: Path) -> None:
    # The defect: this returned True for every project.
    status = _memory_status(tmp_path, _project(tmp_path, graphiti="false"))
    assert status["enabled"] is False, status


def test_a_project_with_graphiti_on_reports_enabled_true(tmp_path: Path) -> None:
    status = _memory_status(tmp_path, _project(tmp_path, graphiti="true"))
    assert status["enabled"] is True, status


def test_a_project_that_says_nothing_falls_back_to_the_deployment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """No per-project setting: the deployment default decides, and it is OFF.

    This is `_flag`'s documented rule, and it is the one that matters for the
    original bug — most projects say nothing, and they were all reading True.
    """
    monkeypatch.delenv("GRAPHITI_ENABLED", raising=False)
    project = _project(tmp_path, graphiti=None)
    assert _memory_status(tmp_path, project)["enabled"] is False

    monkeypatch.setenv("GRAPHITI_ENABLED", "true")
    assert _memory_status(tmp_path, project)["enabled"] is True


def test_the_project_setting_beats_the_deployment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Per-project wins, or the Settings UI is decorative."""
    monkeypatch.setenv("GRAPHITI_ENABLED", "true")
    status = _memory_status(tmp_path, _project(tmp_path, graphiti="false"))
    assert status["enabled"] is False, status


def test_the_other_memory_fields_are_untouched(tmp_path: Path) -> None:
    """`available` answers a different question and must not follow `enabled`.

    A project can have Graphiti switched off and still have session insights on
    disk; collapsing the two would lose that.
    """
    status = _memory_status(tmp_path, _project(tmp_path, graphiti="false"))
    assert "available" in status
    assert "graphitiAvailable" in status
    assert status["sessionInsightsCount"] == 0
    assert os.environ.get("GRAPHITI_ENABLED") in (None, "", "false", "true")
