#!/usr/bin/env python3
"""Build stdout control markers are authenticated by a per-task nonce (#1929).

Agent-authored text (tool output, file contents) reaches the same stdout the
control plane parses. Only a marker carrying the task's own nonce may move the
phase, emit usage or set the rate-limit flag; everything else is plain log text.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from core.auth import get_agent_env_blanks  # noqa: E402
from core.child_env import child_env  # noqa: E402
from server.services import build_backend as bb  # noqa: E402
from server.services.agent_emit import EmitMixin  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402
from server.services.task_phase import TaskPhase  # noqa: E402

from .test_build_backend_kubejob import (  # noqa: E402
    _DATA_ROOT,
    _FakeBatch,
    _make_store,
    _spawn_args,
)
from .test_kubejob_execution_progress import (  # noqa: E402
    SPEC_ID,
    TASK_ID,
    _dispatch_and_stream,
)

NONCE = "n0nce"
USAGE = {"totalInputTokens": 10, "outputTokens": 5, "totalCostUsd": 0.01}
PHASE = '{"phase": "coding", "message": "m"}'
REAL = {
    "phase": f"__EXEC_PHASE__:{NONCE}:{PHASE}",
    "usage": f"__USAGE__:{NONCE}:{json.dumps(USAGE)}",
    "limit": f"__RATE_LIMIT__:{NONCE}:",
}
FORGED = [
    f"__EXEC_PHASE__:{PHASE}",  # no nonce
    f"__EXEC_PHASE__:wrong:{PHASE}",
    f"__EXEC_PHASE__::{PHASE}",  # empty nonce
    f"[PHASE_EVENT] phase=coding message=x",  # noqa: F541 - legacy format
    '{"phase": "coding", "message": "m"}',  # bare JSON
    f"__USAGE__:{json.dumps(USAGE)}",
    f"__USAGE__:wrong:{json.dumps(USAGE)}",
    "__RATE_LIMIT__:",
    "__RATE_LIMIT__:wrong:",
    "You've hit your limit · resets 5pm",  # plain text, was a trigger
]


class _Probe(EmitMixin):
    def __init__(self, nonce: str | None = NONCE) -> None:
        self._task_marker_nonces = {TASK_ID: nonce} if nonce is not None else {}
        self._task_rate_limits: dict = {}
        self._task_current_phases: dict = {}
        self._spec_stderr_logs: dict = {}
        self.usage: list[str] = []
        self.handled: list[str] = []
        self._emit_log = AsyncMock()
        self._emit_progress = AsyncMock()

    async def _emit_live_usage(self, task_id, spec_id, payload):  # noqa: ANN001
        self.usage.append(payload)
        return True


async def _feed(probe: _Probe, lines: list[str]) -> TaskPhase:
    reader = asyncio.StreamReader()
    for ln in lines:
        reader.feed_data(ln.encode() + b"\n")
    reader.feed_eof()
    return await probe._process_output(TASK_ID, reader, spec_id=SPEC_ID)


@pytest.mark.parametrize("nonce", [NONCE, None, ""])
@pytest.mark.parametrize("line", FORGED)
async def test_forged_markers_are_plain_log_text(line, nonce):
    probe = _Probe(nonce)
    out = await probe._handle_output_line(
        TASK_ID, line, current_phase=TaskPhase.PLANNING, spec_id=SPEC_ID
    )
    assert out == TaskPhase.PLANNING
    assert not probe.usage
    assert not probe._task_rate_limits
    probe._emit_progress.assert_not_called()
    probe._emit_log.assert_awaited_once()  # still shown in the log pane


async def test_real_markers_work_in_process():
    probe = _Probe()
    out = await probe._handle_output_line(
        TASK_ID, REAL["usage"], current_phase=TaskPhase.PLANNING, spec_id=SPEC_ID
    )
    assert probe.usage == [json.dumps(USAGE)] and out == TaskPhase.PLANNING
    await probe._handle_output_line(
        TASK_ID, REAL["limit"], current_phase=TaskPhase.PLANNING, spec_id=SPEC_ID
    )
    assert probe._task_rate_limits == {TASK_ID: True}
    out = await probe._handle_output_line(
        TASK_ID, REAL["phase"], current_phase=TaskPhase.PLANNING, spec_id=SPEC_ID
    )
    assert out == TaskPhase.CODING


async def test_real_markers_work_through_the_kubejob_stream(tmp_path):
    service = AgentService()
    service._emit_progress = AsyncMock()
    service._emit_log = AsyncMock()
    service._emit_live_usage = AsyncMock(return_value=True)
    service._task_marker_nonces[TASK_ID] = NONCE
    await _dispatch_and_stream(
        service, tmp_path, [*FORGED, REAL["limit"], REAL["usage"], REAL["phase"]]
    )
    assert service._task_current_phases[TASK_ID] == TaskPhase.CODING
    assert service._task_rate_limits == {TASK_ID: True}
    service._emit_live_usage.assert_awaited_once()
    assert service._emit_progress.await_count == 1


async def test_a_bad_line_never_stops_the_stream():
    probe = _Probe()
    lines = [
        '{"status": "x"}',
        f'__EXEC_PHASE__:{NONCE}:{{"phase": "coding", "subtask_index": "abc"}}',
        f"__EXEC_PHASE__:{NONCE}:not json",
        f"__EXEC_PHASE__:{NONCE}:[1, 2]",
        "boom",
        REAL["limit"],
    ]
    real = probe._handle_output_line

    async def flaky(task_id, line, **kw):  # noqa: ANN001, ANN003
        if line == "boom":
            raise RuntimeError("handler bug")
        return await real(task_id, line, **kw)

    probe._handle_output_line = flaky
    out = await _feed(probe, lines)
    assert probe._task_rate_limits == {TASK_ID: True}, "later line was not handled"
    assert out == TaskPhase.CODING
    assert probe._emit_progress.await_args.args[0].subtask_index is None


def _patched_parser(monkeypatch, error):
    """Run the real ``_patch_sdk_message_parser`` against a fake SDK parser.

    tests/conftest.py replaces ``claude_agent_sdk`` with a mock, so the real
    parser is not importable here; the patch only needs ``parse_message``.
    """
    import core.client as client  # noqa: PLC0415

    parser = types.ModuleType("claude_agent_sdk._internal.message_parser")
    parser.parse_message = lambda _data: types.SimpleNamespace(error=error)
    errors = types.ModuleType("claude_agent_sdk._errors")
    errors.MessageParseError = type("MessageParseError", (Exception,), {})
    for name, mod in {
        "claude_agent_sdk._internal": types.ModuleType("claude_agent_sdk._internal"),
        "claude_agent_sdk._internal.message_parser": parser,
        "claude_agent_sdk._errors": errors,
        "claude_agent_sdk.types": types.SimpleNamespace(SystemMessage=object),
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    # `import a.b.c as x` resolves through getattr, so wire the attributes too.
    internal = sys.modules["claude_agent_sdk._internal"]
    monkeypatch.setattr(internal, "message_parser", parser, raising=False)
    monkeypatch.setattr(
        sys.modules["claude_agent_sdk"], "_internal", internal, raising=False
    )
    client._patch_sdk_message_parser()
    return parser.parse_message


@pytest.mark.parametrize(
    ("extra", "emitted"),
    [("rate_limit", True), (None, False), ("billing_error", False)],
)
def test_patched_parse_emits_rate_limit_marker(extra, emitted, capsys, monkeypatch):
    monkeypatch.setenv("AIFACTORY_MARKER_NONCE", NONCE)
    _patched_parser(monkeypatch, extra)({})
    assert (capsys.readouterr().out == f"__RATE_LIMIT__:{NONCE}:\n") is emitted


def test_nonce_never_reaches_a_child_env(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MARKER_NONCE", NONCE)
    assert "AIFACTORY_MARKER_NONCE" not in child_env(agent=True)
    assert "AIFACTORY_MARKER_NONCE" not in child_env()
    assert get_agent_env_blanks()["AIFACTORY_MARKER_NONCE"] == ""


def test_marker_nonce_env_is_fresh_per_spawn():
    svc = AgentService()
    a = svc._marker_nonce_env("p:a")["AIFACTORY_MARKER_NONCE"]
    b = svc._marker_nonce_env("p:a")["AIFACTORY_MARKER_NONCE"]
    assert len(a) == 32 and a != b, "a respawn must not reuse an older nonce"
    assert svc._task_marker_nonces["p:a"] == b


async def test_kubejob_dispatch_puts_the_nonce_in_the_job_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setattr(bb, "populate_build_worktree", lambda *_a, **_k: None)
    store = await _make_store(tmp_path / "d.db")
    await store.admit("p:s1", _spawn_args("s1"), cap=2, correlation_key="9")
    fake = _FakeBatch()
    await bb.KubeJobBuildBackend(store).dispatch(
        task_id="p:s1",
        project_path=Path(_DATA_ROOT) / "workspaces" / "p",
        spec_id="s1",
        correlation_key="9",
        batch=fake,
        marker_nonce=NONCE,
    )
    env = fake.created[0][1]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "AIFACTORY_MARKER_NONCE", "value": NONCE} in env
    assert {"name": "AIFACTORY_TASK_ID", "value": "p:s1"} in env  # #1972


async def test_nonce_never_reaches_a_log_sink(tmp_path):
    """task_logs.json sits in the agent's worktree; the pane is shown back too."""
    from server.services.task_log_writer import TaskLogWriter  # noqa: PLC0415

    probe = _Probe()
    spec_dir = tmp_path / SPEC_ID
    spec_dir.mkdir()
    writer = TaskLogWriter(spec_dir)
    stderr_file = tmp_path / "spawn_stderr.log"
    probe._spec_stderr_logs[TASK_ID] = stderr_file
    for line in REAL.values():
        for is_stderr in (False, True):
            await probe._handle_output_line(
                TASK_ID,
                line,
                current_phase=TaskPhase.PLANNING,
                is_stderr=is_stderr,
                log_writer=writer,
                spec_id=SPEC_ID,
            )
    assert probe._task_rate_limits == {TASK_ID: True}  # markers still parsed
    shown = [c.args[0].content for c in probe._emit_log.await_args_list]
    assert shown and not any(NONCE in c for c in shown)
    assert (spec_dir / "task_logs.json").is_file() and stderr_file.is_file()
    for f in [stderr_file, *spec_dir.rglob("*")]:
        if f.is_file():
            assert NONCE not in f.read_text(), f


async def test_spec_creation_spawns_with_a_nonce(tmp_path, monkeypatch):
    """The spawn block itself runs (a NameError here broke every spec start)."""
    from server.services import agent_spec_creation as sc  # noqa: PLC0415

    seen: dict = {}

    async def fake_exec(*cmd, env, **_kw):  # noqa: ANN001, ANN003
        seen["env"] = env
        return types.SimpleNamespace(stdout=None, stderr=None, pid=1)

    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sc, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(
        sc.project_settings_store, "mcp_spawn_env", AsyncMock(return_value={})
    )
    monkeypatch.setattr(sc.project_settings_store, "load_project_env", AsyncMock())
    svc = AgentService()
    svc._emit_progress = AsyncMock()
    svc._resolve_claude_token_pooled = lambda _t: (None, None, None)
    svc._kubejob_backend_enabled = lambda: False
    proc = await svc.start_spec_creation("p:s1", tmp_path, "t", "d")
    assert proc.pid == 1
    assert seen["env"]["AIFACTORY_MARKER_NONCE"] == svc._task_marker_nonces["p:s1"]


async def test_build_spawn_env_carries_the_task_id(tmp_path, monkeypatch):
    """#1972: the build subprocess learns the project-scoped task id from env."""
    from server.services import agent_service as agent_mod  # noqa: PLC0415

    seen: dict = {}

    async def fake_exec(*_cmd, env, **_kw):  # noqa: ANN002, ANN003
        seen["env"] = env
        raise RuntimeError("stop after spawn")

    monkeypatch.setattr(agent_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        agent_mod.project_settings_store,
        "mcp_spawn_env",
        AsyncMock(return_value={}),
    )
    svc = AgentService()
    with pytest.raises(RuntimeError, match="stop after spawn"):
        await svc._spawn_task_execution("p:001-x", tmp_path / "proj", "001-x")
    assert seen["env"]["AIFACTORY_TASK_ID"] == "p:001-x"


@pytest.mark.parametrize(
    ("exc", "emitted"),
    [(RuntimeError("429 Too Many Requests"), True), (RuntimeError("boom"), False)],
)
async def test_session_exception_rate_limit_emits_marker(
    exc, emitted, tmp_path, capsys, monkeypatch
):
    import agents.session as session  # noqa: PLC0415

    monkeypatch.setenv("AIFACTORY_MARKER_NONCE", NONCE)

    class _Client:
        async def query(self, _msg):  # noqa: ANN001
            raise exc

    await session.run_agent_session(_Client(), "m", tmp_path)
    out = capsys.readouterr().out
    assert (f"__RATE_LIMIT__:{NONCE}:" in out) is emitted


async def test_nonce_never_reaches_the_kubejob_rmux_feed(tmp_path):
    """Drive the real ``_fan_out`` with the feed the control plane wires in."""
    from unittest.mock import patch  # noqa: PLC0415

    from server.services import build_log_stream as bls  # noqa: PLC0415

    fed: list[bytes] = []
    real_streamer = bls.KubeJobLogStreamer
    service = AgentService()
    service._emit_progress = AsyncMock()
    service._emit_log = AsyncMock()
    service._kubejob_rmux_feed = lambda: lambda _spec, data: fed.append(data)
    service._task_marker_nonces[TASK_ID] = NONCE

    class _Streamer(real_streamer):
        async def stream(self, **_kw):  # noqa: ANN003
            for line in REAL.values():
                await self._fan_out(SPEC_ID, line.encode() + b"\n")

    with (
        patch.object(
            service, "_kubejob_worker_ref", AsyncMock(return_value=("ns", "job"))
        ),
        patch.object(bls, "KubeJobLogStreamer", _Streamer),
    ):
        await service._start_kubejob_log_stream(
            task_id=TASK_ID, project_path=tmp_path, spec_id=SPEC_ID
        )
        task = service._kubejob_log_streamers.get(TASK_ID)
        if task is not None:
            await task
    assert len(fed) == len(REAL)
    assert not any(NONCE.encode() in b for b in fed)
    assert service._task_rate_limits == {TASK_ID: True}  # still parsed


async def test_nonce_never_reaches_the_in_pod_rmux_feed(monkeypatch):
    from server.rmux import integration  # noqa: PLC0415

    fed: list[bytes] = []
    monkeypatch.setattr(integration, "is_enabled", lambda: True)
    monkeypatch.setattr(integration, "feed_if_enabled", lambda _s, d: fed.append(d))
    probe = _Probe()
    await _feed(probe, list(REAL.values()))
    assert len(fed) == len(REAL)
    assert not any(NONCE.encode() in b for b in fed)
    assert probe._task_rate_limits == {TASK_ID: True}
