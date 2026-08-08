#!/usr/bin/env python3
"""Per-worker ``duration_ms`` must be measured, not defaulted to 0 (#1100).

A swarm comparison is about how long each worker took. `token_usage.json`
carried real tokens, cost, provider, model and phase per worker -- and
`duration_ms: 0` for every one of them. 0 is not a fast worker, it is an
unmeasured one, and Factory#345 (cross-model swarm comparison on wall-clock)
divides by it.

Established root cause: the value was NEVER WRITTEN on the serial path, not
written wrongly. `_fold_worker` accumulates correctly and
`parallel_integration.py` brackets its session and passes a real value; the
serial coder simply omitted the kwarg, which defaulted to None, which the fold
then skipped -- leaving the record's initial 0 in place for the life of the run.
A serial build keys its workers by subtask id, so the artifact in the issue
(`main` plus five `subtask-N-M`) is one producer, all zeros.

Two guards here: the fold does the arithmetic, and the omission that caused this
is now a TypeError rather than a zero in an artifact.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.token_attribution import (  # noqa: E402
    PromptSegments,
    TurnUsage,
    record_turn,
    usage_file_path,
)


def _usage(spec_dir: Path) -> dict:
    return json.loads(usage_file_path(spec_dir).read_text())


def test_a_measured_turn_lands_a_real_duration(tmp_path: Path):
    spec_dir = tmp_path / "097-inventory-reservation-service-"
    spec_dir.mkdir()

    record_turn(
        spec_dir,
        PromptSegments(user_prompt="x" * 400),
        TurnUsage(input_tokens=100, output_tokens=10, cost_usd=0.1),
        model="claude-opus-4-8",
        worker_id="subtask-1-1",
        subtask_id="subtask-1-1",
        provider="claude",
        phase="coding",
        duration_ms=41_237,
    )

    rec = _usage(spec_dir)["workers"]["subtask-1-1"]
    assert rec["duration_ms"] == 41_237, (
        "a measured turn must not be reported as an unmeasured one"
    )


def test_durations_sum_across_a_workers_turns(tmp_path: Path):
    """A worker's wall-clock is the sum of its turns, not the last one."""
    spec_dir = tmp_path / "001-sum"
    spec_dir.mkdir()

    for ms in (1_000, 2_500, 700):
        record_turn(
            spec_dir,
            PromptSegments(user_prompt="x" * 400),
            TurnUsage(input_tokens=100, output_tokens=10, cost_usd=0.1),
            model="claude-opus-4-8",
            worker_id="main",
            provider="claude",
            phase="planning",
            duration_ms=ms,
        )

    assert _usage(spec_dir)["workers"]["main"]["duration_ms"] == 4_200


def test_workers_do_not_share_a_duration(tmp_path: Path):
    """Attribution is per worker: a swarm needs a denominator EACH."""
    spec_dir = tmp_path / "002-swarm"
    spec_dir.mkdir()

    for wid, ms in (("subtask-1-1", 12_000), ("subtask-2-1", 31_500)):
        record_turn(
            spec_dir,
            PromptSegments(user_prompt="x" * 400),
            TurnUsage(input_tokens=100, output_tokens=10, cost_usd=0.1),
            model="claude-opus-4-8",
            worker_id=wid,
            subtask_id=wid,
            provider="claude",
            phase="coding",
            duration_ms=ms,
        )

    workers = _usage(spec_dir)["workers"]
    assert workers["subtask-1-1"]["duration_ms"] == 12_000
    assert workers["subtask-2-1"]["duration_ms"] == 31_500


def test_omitting_the_duration_is_a_typeerror_not_a_zero(tmp_path: Path):
    """The guard with teeth: this omission is exactly what shipped the bug.

    `duration_ms` used to default to None, and a None was silently skipped by
    the fold. A caller that forgot it produced a plausible-looking 0 that no
    consumer could distinguish from a real measurement.
    """
    spec_dir = tmp_path / "003-required"
    spec_dir.mkdir()

    with pytest.raises(TypeError, match="duration_ms"):
        record_turn(
            spec_dir,
            PromptSegments(user_prompt="x" * 400),
            TurnUsage(input_tokens=100, output_tokens=10),
            model="claude-opus-4-8",
            worker_id="main",
        )


def _record_turn_call(source: str) -> ast.Call:
    """The `record_turn(...)` call node in a module's source."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "record_turn"
        ):
            return node
    raise AssertionError("no record_turn call found")


@pytest.mark.parametrize(
    "module_name",
    ["agents.coder", "agents.parallel_integration"],
)
def test_both_producers_pass_a_duration(module_name: str):
    """Both real producers must feed the field, not just the parallel one.

    A unit test on `_fold_worker` cannot catch a producer that never calls it
    with a duration -- which is precisely how this shipped: the fold was always
    correct and the serial caller never fed it.
    """
    import importlib

    module = importlib.import_module(module_name)
    call = _record_turn_call(inspect.getsource(module))

    assert "duration_ms" in {kw.arg for kw in call.keywords}, (
        f"{module_name} calls record_turn without duration_ms"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
