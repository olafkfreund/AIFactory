"""Guard tests for the OpenCode CLI runtime provider wiring.

OpenCode is a CLI coding-agent runtime selected with an ``opencode:<provider/model>``
model string.  These tests pin:

  * ``infer_provider_from_model`` routes ``opencode:`` to the ``opencode`` provider
    (and crucially is NOT mis-routed to ``codex`` by the ``"codex" in m`` rule),
  * the factory returns ``OpenCodeAgenticProvider`` for an opencode-prefixed model
    in both agentic and text phases,
  * the ``opencode:`` prefix is stripped before reaching the CLI ``--model`` flag,
  * the default model is resolved from ``OPENCODE_DEFAULT_MODEL`` (NO hardcoded
    free model), and a missing model fails with a clear, actionable error,
  * OpenCode is marked community / self-host tier (NOT enterprise-certified),
  * the non-interactive ``opencode run`` command shape.
"""

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_opencode_prefix_routes_to_opencode_not_codex():
    from phase_config import infer_provider_from_model as infer

    assert infer("opencode:openai/gpt-4o") == "opencode"
    assert infer("opencode:anthropic/claude-sonnet-4-5") == "opencode"
    # The substring "code"/"codex" rule must not hijack the opencode prefix.
    assert infer("gpt-5.3-codex") == "codex"
    assert infer("sonnet") == "claude"


def test_strip_provider_prefix_handles_opencode():
    from phase_config import strip_provider_prefix

    assert strip_provider_prefix("opencode:openai/gpt-4o") == "openai/gpt-4o"
    assert (
        strip_provider_prefix("opencode:anthropic/claude-sonnet-4-5")
        == "anthropic/claude-sonnet-4-5"
    )
    # Non-opencode strings pass through untouched.
    assert strip_provider_prefix("anthropic/claude-sonnet-4-5") == "anthropic/claude-sonnet-4-5"


def test_opencode_alias_and_registry():
    from providers.factory import _resolve_canonical, get_provider

    assert _resolve_canonical("opencode") == "opencode"
    assert _resolve_canonical("open-code") == "opencode"

    provider = get_provider(
        "opencode",
        phase="coding",
        model="opencode:anthropic/claude-sonnet-4-5",
        working_dir="/tmp",
    )
    assert type(provider).__name__ == "OpenCodeAgenticProvider"
    # The opencode: prefix is stripped before reaching the CLI's --model.
    assert provider._model == "anthropic/claude-sonnet-4-5"


def test_opencode_text_phase_reuses_agentic_provider():
    from providers.factory import get_provider

    provider = get_provider(
        "opencode",
        phase="qa",
        model="opencode:anthropic/claude-sonnet-4-5",
        working_dir="/tmp",
    )
    # No separate text-only variant; opencode run returns text fine.
    assert type(provider).__name__ == "OpenCodeAgenticProvider"


def test_opencode_default_model_from_env(monkeypatch):
    """No explicit model -> default resolved from OPENCODE_DEFAULT_MODEL."""
    from providers.opencode_agentic import OpenCodeAgenticProvider

    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "anthropic/claude-sonnet-4-5")
    p = OpenCodeAgenticProvider(model="", working_dir=Path("/tmp"))
    assert p._model == "anthropic/claude-sonnet-4-5"


def test_opencode_no_model_raises_clear_error(monkeypatch):
    """No explicit model and no env override -> clear, actionable run-time error.

    We deliberately do NOT silently fall back to a (possibly dead) free model.
    """
    import pytest
    from providers.opencode_agentic import OpenCodeAgenticProvider

    monkeypatch.delenv("OPENCODE_DEFAULT_MODEL", raising=False)
    p = OpenCodeAgenticProvider(model="", working_dir=Path("/tmp"))
    assert p._model == ""  # nothing resolvable
    p._pending_prompt = "do the thing"
    with pytest.raises(RuntimeError, match=r"opencode:<provider/model>"):
        p._build_command()


def test_opencode_explicit_model_overrides_env(monkeypatch):
    """An explicit opencode:<provider/model> wins over the env default."""
    from providers.opencode_agentic import OpenCodeAgenticProvider

    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "openai/gpt-4o")
    p = OpenCodeAgenticProvider(
        model="opencode:anthropic/claude-sonnet-4-5", working_dir=Path("/tmp")
    )
    assert p._model == "anthropic/claude-sonnet-4-5"


def test_opencode_is_not_enterprise_certified():
    """OpenCode is community / self-host tier, NOT enterprise-certified."""
    from providers import opencode_agentic

    assert opencode_agentic.ENTERPRISE_CERTIFIED is False
    assert opencode_agentic.SUPPORT_TIER == "community"


def test_opencode_command_is_non_interactive_run():
    from providers.opencode_agentic import OpenCodeAgenticProvider

    p = OpenCodeAgenticProvider(
        model="opencode:anthropic/claude-sonnet-4-5", working_dir=Path("/tmp/x")
    )
    p._pending_prompt = "do the thing"
    cmd = p._build_command()
    assert cmd[0:2] == ["opencode", "run"]  # non-interactive run subcommand
    assert "--model" in cmd and "anthropic/claude-sonnet-4-5" in cmd
    # Prompt must be a positional argument, NOT passed via -p.
    # The `run` subcommand does not accept -p; passing it causes opencode to
    # print help and exit 1 without doing any work.  (#286)
    assert "-p" not in cmd
    assert "do the thing" in cmd


def test_opencode_invalid_model_rejected():
    import pytest
    from providers.opencode_agentic import OpenCodeAgenticProvider

    with pytest.raises(ValueError):
        OpenCodeAgenticProvider(model="bad model with spaces")


def test_opencode_run_wraps_stdout_in_assistant_message(monkeypatch):
    """Mock the subprocess so the provider yields one AssistantMessage/TextBlock."""
    from providers import opencode_agentic
    from providers.opencode_agentic import OpenCodeAgenticProvider

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"all done\n", b"")

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    # Pretend the CLI is installed and stub the subprocess spawn.
    monkeypatch.setattr(opencode_agentic.shutil, "which", lambda _: "/usr/bin/opencode")
    monkeypatch.setattr(
        opencode_agentic.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    async def _run():
        p = OpenCodeAgenticProvider(
            model="opencode:anthropic/claude-sonnet-4-5", working_dir=Path("/tmp")
        )
        async with p:
            await p.query("hello")
            msgs = [m async for m in p.receive_response()]
        return msgs

    msgs = asyncio.run(_run())
    assert len(msgs) == 1
    assert type(msgs[0]).__name__ == "AssistantMessage"
    block = msgs[0].content[0]
    assert type(block).__name__ == "TextBlock"
    assert block.text == "all done"


def test_opencode_run_errors_when_cli_missing(monkeypatch):
    import pytest
    from providers import opencode_agentic
    from providers.opencode_agentic import OpenCodeAgenticProvider

    monkeypatch.setattr(opencode_agentic.shutil, "which", lambda _: None)

    async def _run():
        p = OpenCodeAgenticProvider(model="opencode:anthropic/claude-sonnet-4-5")
        await p.query("hello")
        async for _ in p.receive_response():
            pass

    with pytest.raises(RuntimeError, match="OpenCode CLI executable not found"):
        asyncio.run(_run())


def test_opencode_help_page_output_raises_error(monkeypatch):
    """If opencode prints its help page (returncode=1), raise RuntimeError.

    This guards the regression from issue #286 where the provider was passing
    ``-p <prompt>`` to the ``run`` subcommand.  The ``run`` subcommand does not
    accept ``-p``; it prints the help page to stdout and exits with code 1.
    The provider must detect this and raise rather than yielding help text as
    agent output.
    """
    import pytest
    from providers import opencode_agentic
    from providers.opencode_agentic import OpenCodeAgenticProvider

    _HELP_TEXT = (
        b"opencode run [message..]\n\nrun opencode with a message\n"
        b"\nPositionals:\n  message  message to send\n"
    )

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return (_HELP_TEXT, b"")

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(opencode_agentic.shutil, "which", lambda _: "/usr/bin/opencode")
    monkeypatch.setattr(
        opencode_agentic.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    async def _run():
        p = OpenCodeAgenticProvider(
            model="opencode:anthropic/claude-sonnet-4-5", working_dir=Path("/tmp")
        )
        async with p:
            await p.query("implement tictactoe")
            async for _ in p.receive_response():
                pass

    with pytest.raises(RuntimeError, match="help page"):
        asyncio.run(_run())
