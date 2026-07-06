"""Guard tests for the GitHub Copilot agentic provider wiring.

Copilot is a CLI router (claude-sonnet-4.5 / gpt-5).  AIFactory selects it with
a ``copilot:<backend>`` model string.  These tests pin the routing and the
prefix/model handling so a refactor can't silently break Copilot selection or
re-route ``copilot:gpt-5`` to the Codex provider.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_copilot_prefix_routes_to_copilot_not_claude_or_codex():
    from phase_config import infer_provider_from_model as infer

    assert infer("copilot:claude-sonnet-4.6") == "copilot"
    assert infer("copilot:gpt-5.3-codex") == "copilot"
    # Bare backend names must still route to their native providers.
    assert infer("sonnet") == "claude"
    assert infer("gpt-5.3-codex") == "codex"


def test_copilot_alias_and_registry():
    from providers.factory import _resolve_canonical, get_provider

    assert _resolve_canonical("copilot") == "copilot"
    assert _resolve_canonical("github-copilot") == "copilot"

    provider = get_provider(
        "copilot", phase="coding", model="copilot:gpt-5.3-codex", working_dir="/tmp"
    )
    assert type(provider).__name__ == "CopilotAgenticProvider"
    # The copilot: prefix is stripped before reaching the CLI's --model.
    assert provider._model == "gpt-5.3-codex"


def test_unknown_copilot_backend_falls_back():
    from providers.copilot_agentic import CopilotAgenticProvider

    p = CopilotAgenticProvider(
        model="copilot:not-a-real-model", working_dir=Path("/tmp")
    )
    assert p._model == "claude-sonnet-4.6"


def test_copilot_command_is_non_interactive():
    from providers.copilot_agentic import CopilotAgenticProvider

    p = CopilotAgenticProvider(
        model="copilot:gpt-5.3-codex", working_dir=Path("/tmp/x")
    )
    p._pending_prompt = "do the thing"
    cmd = p._build_command()
    assert "--allow-all-tools" in cmd  # non-interactive auto-approve
    assert "-p" in cmd
    assert "--model" in cmd and "gpt-5.3-codex" in cmd
    assert "--add-dir" in cmd and "/tmp/x" in cmd
