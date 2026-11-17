"""Spec runs start and register upload of their resolved transcript."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

import core  # noqa: E402
from runners import spec_runner  # noqa: E402


@pytest.mark.parametrize("pass_spec_dir", [False, True])
def test_transcript_uses_resolved_spec_dir(tmp_path, monkeypatch, pass_spec_dir):
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-test"
    orchestrator = MagicMock(spec_dir=spec_dir)
    orchestrator.run = AsyncMock(return_value=False)
    monkeypatch.setattr(
        spec_runner, "SpecOrchestrator", MagicMock(return_value=orchestrator)
    )
    monkeypatch.setattr("core.git_metadata.record_baseline", MagicMock())
    # Fakes, not the real modules: other test modules leave MagicMocks for
    # task_logger in sys.modules, and the runner imports both at call time.
    transcript = SimpleNamespace(start=MagicMock())
    wf = SimpleNamespace(
        maybe_unpack_workspace=MagicMock(),
        maybe_push_transcripts=MagicMock(),
        install_sigterm_upload=MagicMock(),
    )
    monkeypatch.setitem(
        sys.modules, "task_logger", SimpleNamespace(transcript=transcript)
    )
    monkeypatch.setitem(sys.modules, "task_logger.transcript", transcript)
    monkeypatch.setattr(core, "workspace_fetch", wf, raising=False)
    monkeypatch.setitem(sys.modules, "core.workspace_fetch", wf)
    register = MagicMock()
    monkeypatch.setattr("atexit.register", register)
    argv = ["spec_runner.py", "--task", "test", "--project-dir", str(tmp_path)]
    if pass_spec_dir:
        argv.extend(["--spec-dir", str(spec_dir)])
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        spec_runner.main()

    assert exc.value.code == 1
    transcript.start.assert_called_once_with(spec_dir)
    register.assert_called_once_with(wf.maybe_push_transcripts, spec_dir.name)
    wf.install_sigterm_upload.assert_called_once_with(spec_dir.name)
