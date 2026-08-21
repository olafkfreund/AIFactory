#!/usr/bin/env python3
"""A control-plane model fallback must take effect and stay visible (#1374).

The reported defect: a benchmark leg pinned to ``openai-compatible:gpt-oss:120b``
hit an HTTP 500, the web-server "fell back to sonnet", and ``token_usage.json``
still credited the pinned model at $0. Two independent holes produced that:

1. the fallback swaps ``--model`` on the child's command line, but a CLI model is
   priority 3 in ``phase_config._resolve_phase_model`` -- BELOW ``pinnedModel``
   and the auto profile's ``phaseModels``, which is exactly how the benchmark
   pins its models. The swapped flag was outranked and the retry re-resolved the
   model that had just failed;
2. nothing carried the fallback into the artefact, so even a fallback that DID
   take would read as a clean run of whichever model ran last.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "apps" / "backend", _ROOT / "apps" / "web-server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.token_attribution import (  # noqa: E402
    PromptSegments,
    TurnUsage,
    record_turn,
    usage_file_path,
)
from phase_config import get_phase_model  # noqa: E402

PINNED = "openai-compatible:gpt-oss:120b"


def _pinned_spec(tmp_path: Path) -> Path:
    """A spec pinned the way the benchmark pins it: auto profile + phaseModels."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "task_metadata.json").write_text(
        json.dumps(
            {
                "isAutoProfile": True,
                "phaseModels": dict.fromkeys(
                    ("spec", "planning", "coding", "qa"), PINNED
                ),
            }
        )
    )
    return spec_dir


def test_pinned_phase_model_wins_without_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard rail: absent a fallback, the pin still outranks the CLI model."""
    monkeypatch.delenv("AIFACTORY_FALLBACK_MODEL", raising=False)
    assert get_phase_model(_pinned_spec(tmp_path), "coding", "sonnet") == PINNED


def test_fallback_outranks_the_pinned_phase_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model the control plane fell back to is the model that resolves."""
    monkeypatch.setenv("AIFACTORY_FALLBACK_MODEL", "sonnet")
    resolved = get_phase_model(_pinned_spec(tmp_path), "coding", "sonnet")
    assert resolved != PINNED, (
        "the pinned model outranked the fallback: the retry re-runs the model "
        "that just failed, and the accounting then credits it"
    )
    assert "sonnet" in resolved


def _record(spec_dir: Path) -> dict:
    record_turn(
        spec_dir,
        PromptSegments(user_prompt="build it"),
        TurnUsage(input_tokens=100, output_tokens=50, cost_usd=0.0),
        model="claude-sonnet-5",
        worker_id="main",
        provider="claude",
        duration_ms=1234,
    )
    return json.loads(usage_file_path(spec_dir).read_text())


def test_usage_file_names_the_displaced_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """token_usage.json must show the fallback, not just the model that ran."""
    monkeypatch.setenv("AIFACTORY_FALLBACK_FROM", PINNED)
    data = _record(tmp_path)

    assert data["workers"]["main"]["fallbackFrom"] == PINNED
    assert data["fallbacks"] == [
        {"from": PINNED, "to": "claude-sonnet-5", "workerId": "main"}
    ]


def test_a_clean_run_records_no_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fallback -> an explicit empty list, never a missing key."""
    monkeypatch.delenv("AIFACTORY_FALLBACK_FROM", raising=False)
    data = _record(tmp_path)

    assert data["fallbacks"] == []
    assert "fallbackFrom" not in data["workers"]["main"]


async def _retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple:
    """Drive the real retry with the subprocess spawn stubbed out."""
    from server.services.agent_service import AgentService

    svc = AgentService()
    svc._task_profiles["t1"] = {"model": PINNED, "attempt": 1}
    seen: dict = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["env"] = kwargs.get("env")
        return object()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    proc = await svc._retry_task_with_fallback_model(
        "t1", tmp_path, "spec-1", ["run.py", "--model", PINNED], {"PATH": "/usr/bin"}
    )
    return proc, seen


async def test_retry_forces_the_fallback_model_on_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is told, in a way the pin cannot outrank, what it now runs."""
    monkeypatch.delenv("AIFACTORY_MODEL_FALLBACK", raising=False)
    proc, seen = await _retry(monkeypatch, tmp_path)

    assert proc is not None
    assert seen["env"]["AIFACTORY_FALLBACK_MODEL"] == "sonnet"
    assert seen["env"]["AIFACTORY_FALLBACK_FROM"] == PINNED
    assert seen["env"]["PATH"] == "/usr/bin"  # the inherited env survives


async def test_strict_mode_refuses_to_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A benchmark can demand the pinned model or a visible failure."""
    monkeypatch.setenv("AIFACTORY_MODEL_FALLBACK", "off")
    proc, seen = await _retry(monkeypatch, tmp_path)

    assert proc is None
    assert seen == {}, "strict mode still spawned a fallback subprocess"
