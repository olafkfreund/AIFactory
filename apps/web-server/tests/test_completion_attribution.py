"""Factory#1960: ``agent_identity`` / ``originator`` on every emitted event."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.services import completion as comp  # noqa: E402

_WHO = {"agent_identity": "agent:aifactory/k1", "originator": "github:octocat"}
_NOBODY = {"agent_identity": None, "originator": None}


def _spec(tmp_path: Path, text: str | None) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir()
    if text is not None:
        (spec / "requirements.json").write_text(text)
    return spec


def test_attribution_read_from_requirements(tmp_path: Path) -> None:
    spec = _spec(tmp_path, json.dumps({**_WHO, "title": "t"}))
    assert comp._read_attribution(spec) == _WHO


@pytest.mark.parametrize("text", [None, "{not json", "[]", '{"originator": 3}'])
def test_attribution_is_null_when_missing_or_garbled(
    tmp_path: Path, text: str | None
) -> None:
    assert comp._read_attribution(_spec(tmp_path, text)) == _NOBODY


def test_every_builder_carries_both_keys() -> None:
    common: dict[str, Any] = {"task_id": "p:s", "spec_id": "s", "issue_number": None}
    worker: dict[str, Any] = {"worker_id": "main"}
    events = [
        comp.build_completion_event(status="done", **common, **_WHO),
        comp.build_worker_event(worker=worker, **common, **_WHO),
        comp.build_worker_progress_event(worker=worker, **common, **_WHO),
    ]
    bare = [
        comp.build_completion_event(status="done", **common),
        comp.build_worker_event(worker=worker, **common),
        comp.build_worker_progress_event(worker=worker, **common),
    ]
    assert [{k: e[k] for k in _WHO} for e in events] == [_WHO] * 3
    assert [{k: e[k] for k in _WHO} for e in bare] == [_NOBODY] * 3


def test_emitters_stamp_the_spec_attribution(tmp_path: Path) -> None:
    spec = _spec(tmp_path, json.dumps(_WHO))
    kw: dict[str, Any] = {"task_id": "p:s", "spec_id": "s", "project_id": "p"}
    worker: dict[str, Any] = {"worker_id": "w1", "total_tokens": 5}
    events = [
        comp.emit_terminal_completion(spec, status="done", **kw),
        comp.emit_worker_completion(spec_dir=spec, worker=worker, **kw),
        comp.emit_worker_progress(spec_dir=spec, worker=worker, **kw),
        comp.emit_usage_snapshot(
            spec, status="running", usage={"total_tokens": 1}, **kw
        ),
    ]
    for event in events:
        assert event is not None
        assert {k: event[k] for k in _WHO} == _WHO
