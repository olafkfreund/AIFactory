"""Tests for the injection_scan completion-envelope stamp (#805 / Factory#273)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.services.completion import (  # noqa: E402
    _read_injection_scan,
    build_completion_event,
    emit_terminal_completion,
)


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "requirements.json").write_text(json.dumps({}))
    return spec


def _write_scan(spec: Path, **overrides) -> dict:
    data = {
        "verdict": "flagged",
        "mode": "on",
        "matched": [{"pattern": "curl_pipe_shell", "file": "README.md"}],
    }
    data.update(overrides)
    (spec / "injection_scan.json").write_text(json.dumps(data))
    return data


def test_envelope_includes_injection_scan_when_supplied() -> None:
    scan = {"verdict": "flagged", "mode": "on", "matched": []}
    ev = build_completion_event(
        task_id="t",
        spec_id="s",
        status="human_review",
        issue_number=1,
        injection_scan=scan,
    )
    assert ev["injection_scan"] == scan


def test_envelope_omits_injection_scan_when_absent() -> None:
    ev = build_completion_event(task_id="t", spec_id="s", status="done", issue_number=1)
    assert "injection_scan" not in ev


def test_read_injection_scan_roundtrip(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    data = _write_scan(spec)
    assert _read_injection_scan(spec) == data


def test_read_injection_scan_absent_or_invalid(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert _read_injection_scan(spec) is None  # no file
    _write_scan(spec, verdict="bogus")
    assert _read_injection_scan(spec) is None  # unknown verdict rejected
    (spec / "injection_scan.json").write_text("not json")
    assert _read_injection_scan(spec) is None  # unreadable


def test_emit_terminal_completion_stamps_verdict(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIFACTORY_COMPLETION_WEBHOOK", raising=False)
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    spec = _spec(tmp_path)
    _write_scan(spec, verdict="flagged")
    ev = emit_terminal_completion(
        spec, task_id="p:s", project_id="p", spec_id="s", status="human_review"
    )
    assert ev["injection_scan"]["verdict"] == "flagged"
    assert ev["injection_scan"]["matched"][0]["pattern"] == "curl_pipe_shell"
