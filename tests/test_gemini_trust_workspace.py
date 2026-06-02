"""Regression guard: the Antigravity agentic provider must trust the workspace.

`antigravity --yolo` refuses every tool call in an "untrusted" directory —
which our isolated git worktrees always are — so the coder silently wrote zero
files and all coding subtasks failed. The provider must pass
GEMINI_CLI_TRUST_WORKSPACE=true in the subprocess env so --yolo can edit files.
(The env var keeps its legacy name across the gemini-cli -> antigravity-cli
rename — that is the name the binary actually reads.)
"""
from pathlib import Path

PROVIDER = (
    Path(__file__).resolve().parents[1]
    / "apps" / "backend" / "providers" / "antigravity_agentic.py"
)


def test_antigravity_agentic_passes_trust_workspace_env():
    src = PROVIDER.read_text()
    assert "GEMINI_CLI_TRUST_WORKSPACE" in src, \
        "antigravity_agentic must set GEMINI_CLI_TRUST_WORKSPACE so --yolo can edit files"
    assert "env=env" in src, \
        "the trust env must actually be passed to create_subprocess_exec(env=...)"
