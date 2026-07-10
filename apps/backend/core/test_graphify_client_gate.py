"""Unit test for the opt-in graphify code-graph gate in create_client.

Verifies the tool is exposed ONLY when: agent is the coder, the
AIFACTORY_GRAPHIFY_ENABLED flag is "true", and the graph file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core.client import _graphify_server_config


def _graph(tmp_path: Path) -> Path:
    g = tmp_path / "graphify-out" / "graph.json"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("{}")
    return g


def test_enabled_for_coder_when_flag_and_graph_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_GRAPHIFY_ENABLED", "true")
    cfg = _graphify_server_config("coder", _graph(tmp_path))
    assert cfg == {"command": "graphify-mcp", "args": [str(_graph(tmp_path))]}


def test_off_when_flag_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_GRAPHIFY_ENABLED", raising=False)
    assert _graphify_server_config("coder", _graph(tmp_path)) is None


def test_off_for_non_coder_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_GRAPHIFY_ENABLED", "true")
    assert _graphify_server_config("planner", _graph(tmp_path)) is None
    assert _graphify_server_config("qa_reviewer", _graph(tmp_path)) is None


def test_off_when_graph_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_GRAPHIFY_ENABLED", "true")
    missing = tmp_path / "graphify-out" / "graph.json"
    assert _graphify_server_config("coder", missing) is None
