"""Regression guard: the Gemini agentic provider must trust the workspace.

`gemini --yolo` refuses every tool call in an "untrusted" directory — which our
isolated git worktrees always are — so the coder silently wrote zero files and
all coding subtasks failed. The provider must pass GEMINI_CLI_TRUST_WORKSPACE=true
in the subprocess env so --yolo can edit files.
"""
from pathlib import Path

PROVIDER = Path(__file__).resolve().parents[1] / "apps" / "backend" / "providers" / "gemini_agentic.py"


def test_gemini_agentic_passes_trust_workspace_env():
    src = PROVIDER.read_text()
    assert "GEMINI_CLI_TRUST_WORKSPACE" in src, \
        "gemini_agentic must set GEMINI_CLI_TRUST_WORKSPACE so --yolo can edit files"
    assert "env=env" in src, \
        "the trust env must actually be passed to create_subprocess_exec(env=...)"
