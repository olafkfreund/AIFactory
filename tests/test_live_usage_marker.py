"""Live token-usage over the log stream (#1249).

Under ``AIFACTORY_BUILD_BACKEND=kubejob`` the build runs in a Job whose
``/work`` is an ephemeral emptyDir, so ``token_usage.json`` reaches the control
plane only when it is pushed back at the END. The old live-cost emitter lived in
``agent_worktree_sync._sync_worktree_files``, reachable ONLY through
``process_monitor.monitor_process``, which needs a local subprocess the kubejob
backend never creates — so the cockpit showed no cost accruing at all.

Cost now rides the stdout stream that #1229 already follows.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services.completion import read_usage, usage_from_aggregate  # noqa: E402

AGG = {
    "totalInputTokens": 1000,
    "outputTokens": 250,
    "totalCostUsd": 0.0123,
    "model": "claude-opus-5",
}


def _emit_module():
    import core.phase_event as pe  # noqa: PLC0415

    pe._last_usage_emit = 0.0  # reset the process-wide throttle clock
    return pe, pe.emit_usage


def test_marker_is_emitted_and_parseable():
    pe, emit_usage = _emit_module()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert emit_usage(AGG) is True
    line = buf.getvalue().strip()
    assert line.startswith(pe.USAGE_MARKER_PREFIX)
    assert json.loads(line[len(pe.USAGE_MARKER_PREFIX) :]) == AGG


def test_throttled_so_a_chatty_build_cannot_flood_the_cockpit():
    _pe, emit_usage = _emit_module()
    with contextlib.redirect_stdout(io.StringIO()):
        assert emit_usage(AGG) is True
        # Attribution folds a turn per model response; without a throttle this
        # would emit on every one of them.
        assert emit_usage(AGG) is False
        assert emit_usage(AGG, force=True) is True


def test_non_dict_payload_is_refused():
    _pe, emit_usage = _emit_module()
    with contextlib.redirect_stdout(io.StringIO()):
        assert emit_usage("not-a-dict", force=True) is False  # type: ignore[arg-type]


def test_marker_path_and_file_path_map_identically(tmp_path: Path):
    """The whole point of splitting ``usage_from_aggregate`` out.

    Two copies of this mapping is how one of them goes stale — the argument
    #1229 makes for phase parsing, applied to cost.
    """
    (tmp_path / "token_usage.json").write_text(json.dumps(AGG))
    assert read_usage(tmp_path) == usage_from_aggregate(AGG)


def test_zero_usage_emits_nothing_rather_than_a_zero_cost_event():
    # A zero-cost event would render in the cockpit as a real measurement of
    # zero, which is worse than showing nothing yet.
    assert usage_from_aggregate({"totalInputTokens": 0, "outputTokens": 0}) is None
    assert usage_from_aggregate(None) is None
    assert usage_from_aggregate("nonsense") is None  # type: ignore[arg-type]


def test_budget_block_survives_on_the_file_path_and_is_omitted_live(tmp_path: Path):
    """The budget block needs the spec dir; the live path has none.

    Omitting it there is the documented no-budget-set behaviour, not a new gap —
    but the FILE path must keep producing it, or this refactor quietly drops the
    #45 P2 cost-budget warning surface.
    """
    (tmp_path / "token_usage.json").write_text(json.dumps(AGG))
    (tmp_path / "task_metadata.json").write_text(json.dumps({"budgetUsd": 0.01}))
    assert "budget" in (read_usage(tmp_path) or {})
    assert "budget" not in (usage_from_aggregate(AGG) or {})


@pytest.mark.asyncio
async def test_handler_turns_a_marker_into_a_running_cost_event(monkeypatch):
    """The consumer half, on the SHARED handler both backends drive."""
    from server.services.agent_emit import _USAGE_MARKER, EmitMixin

    captured: dict = {}

    def fake_emit(spec_dir, *, task_id, project_id, spec_id, status, usage=None):
        captured.update(
            {"task_id": task_id, "spec_id": spec_id, "status": status, "usage": usage}
        )
        return {"ok": True}

    monkeypatch.setattr(
        "server.services.completion.emit_usage_snapshot", fake_emit, raising=True
    )
    monkeypatch.setattr(
        "server.routes.projects.load_projects", lambda: {"p1": {"path": "/tmp/x"}}
    )

    mixin = EmitMixin()
    line = _USAGE_MARKER + json.dumps(AGG)
    assert await mixin._emit_live_usage("p1:001-spec", "001-spec", line) is True
    assert captured["status"] == "running"
    assert captured["task_id"] == "p1:001-spec"
    assert captured["usage"]["cost_usd"] == pytest.approx(0.0123)


def test_record_turn_actually_emits_the_marker(tmp_path: Path):
    """The PRODUCER half, driven through the real ``record_turn``.

    Without this, deleting the ``emit_usage(agg)`` call leaves every other test
    in this file green: the mapping still works, the handler still routes, and
    nothing would ever be emitted in production. Mutation-checked.
    """
    from agents.token_attribution import PromptSegments, TurnUsage, record_turn

    pe, _ = _emit_module()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        record_turn(
            tmp_path,
            PromptSegments(system_prompt="sys", user_prompt="do the thing"),
            TurnUsage(input_tokens=800, output_tokens=200, cost_usd=0.01),
            model="claude-opus-5",
            duration_ms=1234,
        )
    markers = [
        ln for ln in buf.getvalue().split("\n") if ln.startswith(pe.USAGE_MARKER_PREFIX)
    ]
    assert markers, "record_turn wrote the file but emitted no live usage marker"
    payload = json.loads(markers[0][len(pe.USAGE_MARKER_PREFIX) :])
    assert payload["outputTokens"] == 200
    # And the durable file is still written — the marker is additive, not a
    # replacement for the record the push-back carries at completion.
    assert (tmp_path / "token_usage.json").exists()


@pytest.mark.asyncio
async def test_the_REAL_handler_routes_a_usage_line(monkeypatch):
    """Drive ``_handle_output_line``, not the helper.

    Written because the helper-level test above passes with the production
    branch in ``_handle_output_line`` disabled — the marker would arrive and be
    treated as an ordinary log line, exactly the silent no-op #1249 is about.
    Mutation-checked: stub out that branch and THIS test fails.
    """
    from server.services.agent_emit import _USAGE_MARKER, EmitMixin
    from server.services.task_phase import TaskPhase

    seen: list[str] = []

    class _Probe(EmitMixin):
        async def _emit_log(self, log):  # noqa: ANN001 - test double
            return None

        async def _emit_live_usage(self, task_id, spec_id, line):  # noqa: ANN001
            seen.append(line)
            return True

    probe = _Probe()
    monkeypatch.setattr(
        probe, "_is_rate_limit_line", lambda _line: False, raising=False
    )

    out = await probe._handle_output_line(
        "p1:001-spec",
        _USAGE_MARKER + json.dumps(AGG),
        current_phase=TaskPhase.CODING,
        spec_id="001-spec",
    )
    assert seen, "the __USAGE__ line never reached the live-usage path"
    # A usage marker must not disturb the phase the build is in.
    assert out == TaskPhase.CODING


@pytest.mark.asyncio
async def test_handler_survives_a_malformed_marker(monkeypatch):
    from server.services.agent_emit import _USAGE_MARKER, EmitMixin

    mixin = EmitMixin()
    assert await mixin._emit_live_usage("p1:s", "s", _USAGE_MARKER + "{broken") is False
