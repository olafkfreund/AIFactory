"""emit_usage_snapshot: running cost reaches the cockpit even for a non-terminal
(human_review) stop — cost accrues continuously, not only at terminal completion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # web-server deps; installed in CI's backend gate

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services import completion  # noqa: E402


def test_emit_usage_snapshot_emits_usage_bearing_nonterminal_event(
    tmp_path, monkeypatch
):
    sent = []
    monkeypatch.setattr(completion, "notify_completion", lambda e, **k: sent.append(e))
    monkeypatch.setattr(
        completion,
        "read_usage",
        lambda _sd: {
            "input_tokens": 10000,
            "output_tokens": 2345,
            "total_tokens": 12345,
            "cost_usd": 0.42,
            "model": "claude-sonnet-4-6",
        },
    )
    ev = completion.emit_usage_snapshot(
        tmp_path,
        task_id="p:034-x",
        project_id="p",
        spec_id="034-x",
        status="human_review",
    )
    assert ev is not None
    assert len(sent) == 1
    # The event carries the accrued usage and a NON-terminal status.
    assert sent[0]["usage"]["total_tokens"] == 12345
    assert sent[0]["status"] == "human_review"


def test_emit_usage_snapshot_no_usage_is_noop(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(completion, "notify_completion", lambda e, **k: sent.append(e))
    monkeypatch.setattr(completion, "read_usage", lambda _sd: None)
    ev = completion.emit_usage_snapshot(
        tmp_path, task_id="p:s", project_id="p", spec_id="s", status="human_review"
    )
    assert ev is None
    assert sent == []  # nothing to report → no emit
