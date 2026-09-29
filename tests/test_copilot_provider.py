"""Guard tests for the GitHub Copilot agentic provider wiring.

Copilot is a CLI router (claude-sonnet-4.5 / gpt-5).  AIFactory selects it with
a ``copilot:<backend>`` model string.  These tests pin the routing and the
prefix/model handling so a refactor can't silently break Copilot selection or
re-route ``copilot:gpt-5`` to the Codex provider.
"""

import sys
from pathlib import Path

import pytest

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


# --------------------------------------------------------------------------- #
# #1607: these tests exercise provider RESOLUTION, not operator policy.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _enable_every_runtime(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Opt this module's tests into every runtime.

    The factory now enforces the RFC-0014 operator allowlist, so constructing a
    non-claude provider requires the operator to have enabled it. Opting in here,
    rather than blanket-enabling in conftest, keeps the gate load-bearing in every
    other test — where an unexpected refusal should still be a failure.
    """
    import core.runtime_gating as rg

    monkeypatch.setenv(
        rg.ALLOWLIST_ENV, ",".join(sorted(rg.known_runtimes() - rg.MANUAL_ENABLE_ONLY))
    )
