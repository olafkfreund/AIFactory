"""Regression guard for the Codex agentic provider (#codex):

1. It must resolve the binary even when 'codex' is only a shell alias
   (fall back to 'codex-cli').
2. It must drain stderr concurrently — otherwise a long coding session fills
   the 64 KB stderr pipe, codex deadlocks, and the build sees
   "(no output from Codex MCP)" with no files written.
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "apps" / "backend"
       / "providers" / "codex_agentic.py").read_text()


def test_codex_binary_falls_back_to_codex_cli():
    assert 'shutil.which("codex-cli")' in SRC


def test_codex_drains_stderr_concurrently():
    assert "_drain_stderr" in SRC and "create_task" in SRC, \
        "stderr must be drained concurrently to avoid the 64KB pipe deadlock"
