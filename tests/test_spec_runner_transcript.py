"""Spec runs start and register upload of their resolved transcript."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

from core import workspace_fetch  # noqa: E402
from runners import spec_runner  # noqa: E402
from task_logger import transcript  # noqa: E402


@pytest.mark.parametrize("pass_spec_dir", [False, True])
def test_transcript_uses_resolved_spec_dir(tmp_path, monkeypatch, pass_spec_dir):
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-test"
    orchestrator = MagicMock(spec_dir=spec_dir)
    orchestrator.run = AsyncMock(return_value=False)
    monkeypatch.setattr(
        spec_runner, "SpecOrchestrator", MagicMock(return_value=orchestrator)
    )
    monkeypatch.setattr(workspace_fetch, "maybe_unpack_workspace", MagicMock())
    monkeypatch.setattr("core.git_metadata.record_baseline", MagicMock())
    start = MagicMock()
    register = MagicMock()
    install = MagicMock()
    monkeypatch.setattr(transcript, "start", start)
    monkeypatch.setattr("atexit.register", register)
    monkeypatch.setattr(workspace_fetch, "install_sigterm_upload", install)
    argv = ["spec_runner.py", "--task", "test", "--project-dir", str(tmp_path)]
    if pass_spec_dir:
        argv.extend(["--spec-dir", str(spec_dir)])
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        spec_runner.main()

    assert exc.value.code == 1
    start.assert_called_once_with(spec_dir)
    register.assert_called_once_with(
        workspace_fetch.maybe_push_transcripts, spec_dir.name
    )
    install.assert_called_once_with(spec_dir.name)
