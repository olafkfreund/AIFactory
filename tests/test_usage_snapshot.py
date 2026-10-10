"""emit_usage_snapshot: running cost reaches the cockpit even for a non-terminal
(human_review) stop — cost accrues continuously, not only at terminal completion."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # web-server deps; installed in CI's backend gate

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services import completion, completion_orchestration  # noqa: E402


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


# ---------------------------------------------------------------------------
# #1633 C6: token_usage.json is untrusted input.
# ---------------------------------------------------------------------------


def _ok(**kw):
    return {"totalInputTokens": 10, "outputTokens": 5, **kw}


_W = {"phase": "coding", "input_tokens": 1, "output_tokens": 1}

_BAD_CASES = {
    "nan_cost": _ok(totalCostUsd=float("nan")),
    "nan_tokens": _ok(totalInputTokens=float("nan")),
    "inf_tokens": _ok(totalInputTokens=float("inf")),
    "neg_inf_cost": _ok(totalCostUsd=float("-inf")),
    "negative": {"totalInputTokens": -5, "outputTokens": 10},
    "abc": _ok(outputTokens="abc"),
    "bool": {"totalInputTokens": True, "outputTokens": 0},
    "over_cap_tokens": _ok(totalInputTokens=10**12 + 1),
    "over_cap_cost": _ok(totalCostUsd=1e6 + 1),
    "zero": {"totalInputTokens": 0, "outputTokens": 0},
    "worker_nan": _ok(workers={"w": {**_W, "cost_usd": float("nan")}}),
    "worker_duration_over_cap": _ok(workers={"w": {**_W, "duration_ms": 10**10 + 1}}),
}


@pytest.mark.parametrize("case", list(_BAD_CASES))
def test_usage_rejects_bad_values(case):
    assert completion.usage_from_aggregate(_BAD_CASES[case]) is None


def test_usage_cap_boundary_accepted():
    u = completion.usage_from_aggregate(
        {
            "totalInputTokens": 10**12,
            "outputTokens": 10**12,
            "totalCostUsd": 1e6,
            "workers": {"w": {**_W, "duration_ms": 10**10}},
        }
    )
    assert u is not None
    assert isinstance(u["input_tokens"], int)
    assert isinstance(u["output_tokens"], int)
    assert isinstance(u["total_tokens"], int)
    assert u["workers"][0]["duration_ms"] == 10**10
    assert isinstance(u["workers"][0]["duration_ms"], int)


def test_usage_none_counts_as_zero():
    u = completion.usage_from_aggregate(_ok(outputTokens=None))
    assert u is not None
    assert u["output_tokens"] == 0
    assert isinstance(u["cost_usd"], float)  # 0 == 0.0, so check the type
    assert u["cost_usd"] == 0.0


def test_usage_model_truncated_to_128():
    u = completion.usage_from_aggregate(_ok(model="m" * 300))
    assert u is not None
    assert len(u["model"]) == 128


def test_worker_strings_truncated_and_non_str_dropped(monkeypatch):
    monkeypatch.setattr(completion, "_emit_worker_metrics", lambda _w: None)
    long = "x" * 300
    u = completion.usage_from_aggregate(
        _ok(
            workers={
                "w": {
                    "worker_id": long,
                    "provider": long,
                    "model": long,
                    "routing_tier": long,
                    "phase": 5,
                    "input_tokens": 1,
                    "output_tokens": 1,
                }
            }
        )
    )
    assert u is not None
    w = u["workers"][0]
    for k in ("worker_id", "provider", "model", "routing_tier"):
        assert len(w[k]) == 128, k
    assert w["phase"] is None


def test_workers_limited_to_256(monkeypatch):
    monkeypatch.setattr(completion, "_emit_worker_metrics", lambda _w: None)
    workers = {f"w{i:03d}": {**_W, "model": "m"} for i in range(300)}
    u = completion.usage_from_aggregate(_ok(workers=workers))
    assert u is not None
    assert len(u["workers"]) == 256
    assert u["workers"][0]["worker_id"] == "w000"
    assert u["workers"][-1]["worker_id"] == "w255"
    assert u["by_model"]["m"]["workers"] == 256


AGG = {
    "totalInputTokens": 1000,
    "outputTokens": 250,
    "totalTokens": 1250,
    "totalCostUsd": 0.0123456789,
    "model": "claude-sonnet-4-6",
    "cacheReadTokens": 40,
    "cacheCreationTokens": 2,
    "workers": {
        "w2": {
            "worker_id": "w2",
            "phase": "coding",
            "subtask_id": "1.1",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_tokens": 600,
            "output_tokens": 100,
            "cost_usd": 0.006,
            "duration_ms": 1500,
            "routing_tier": "standard",
        },
        "w1": {
            "phase": "planning",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_tokens": 400,
            "output_tokens": 150,
            "total_tokens": 550,
            "cost_usd": 0.0063456789,
            "duration_ms": 900,
        },
    },
}

GOLDEN = (
    '{"by_model": {"claude-sonnet-4-6": {"billing_mode": "unknown", "cost_usd": 0.012346, "duration_ms": 2400, "input_tokens": 1000, "output_tokens": 250, "total_tokens": 1250, "workers": 2}}, '
    '"by_provider": {"anthropic": {"billing_mode": "unknown", "cost_usd": 0.012346, "duration_ms": 2400, "input_tokens": 1000, "output_tokens": 250, "total_tokens": 1250, "workers": 2}}, '
    '"cache_creation_tokens": 2, "cache_read_tokens": 40, "cost_usd": 0.012346, "input_tokens": 1000, "model": "claude-sonnet-4-6", "output_tokens": 250, "total_tokens": 1250, '
    '"workers": [{"billing_mode": "unknown", "cost_usd": 0.006346, "duration_ms": 900, "input_tokens": 400, "model": "claude-sonnet-4-6", "output_tokens": 150, "phase": "planning", "provider": "anthropic", "subtask_id": null, "total_tokens": 550, "worker_id": "w1"}, '
    '{"billing_mode": "unknown", "cost_usd": 0.006, "duration_ms": 1500, "input_tokens": 600, "model": "claude-sonnet-4-6", "output_tokens": 100, "phase": "coding", "provider": "anthropic", "routing_tier": "standard", "subtask_id": "1.1", "total_tokens": 700, "worker_id": "w2"}]}'
)


def test_valid_block_is_byte_identical(monkeypatch):
    monkeypatch.setattr(completion, "_emit_worker_metrics", lambda _w: None)
    assert json.dumps(completion.usage_from_aggregate(AGG), sort_keys=True) == GOLDEN


_BAD_FILE = '{"totalInputTokens": NaN, "outputTokens": 5}'


def test_bad_usage_file_still_sends_terminal_event_and_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    (tmp_path / "token_usage.json").write_text(_BAD_FILE)
    sent = []
    monkeypatch.setattr(
        completion, "notify_completion", lambda e, **k: sent.append(e) or True
    )
    import asyncio

    asyncio.run(
        completion_orchestration.run_terminal_completion(
            spec_dir=tmp_path,
            project_path=tmp_path,
            spec_id="s",
            task_id="p:s",
            backend_path=None,
            is_terminal=True,
            is_completed=False,
            terminal_status="failed",
            logger=logging.getLogger("t"),
        )
    )
    assert [e["status"] for e in sent] == ["failed"]
    assert "usage" not in sent[0]
    assert (tmp_path / ".terminal_completion_emitted").exists()


def test_bad_usage_file_snapshot_is_noop(tmp_path, monkeypatch):
    (tmp_path / "token_usage.json").write_text(_BAD_FILE)
    sent = []
    monkeypatch.setattr(completion, "notify_completion", lambda e, **k: sent.append(e))
    ev = completion.emit_usage_snapshot(
        tmp_path, task_id="p:s", project_id="p", spec_id="s", status="failed"
    )
    assert ev is None
    assert sent == []
