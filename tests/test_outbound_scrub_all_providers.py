"""Outbound PII scrub covers EVERY provider adapter, not just one (#1128).

#1010 made the pre-send scrub default-on but wired it into
``OpenAICompatibleProvider`` alone. The agentic path -- the dominant
coding path -- and every CLI adapter sent prompts verbatim, so the
Factory#320 acceptance criterion "PII is scrubbed from outbound LLM
calls" read as satisfied while covering a minority of calls.

These tests pin the fix at the seam it was put at: ``BaseLLMProvider``
wraps every subclass's ``query()``. They are written against the wire
(the HTTP payload, the CLI argv, the forwarded SDK prompt) rather than
against the wrapper, so a regression that removes the chokepoint fails
them regardless of how the scrub is re-implemented.

A second family exists that #1128 did not name: ``agents/coder.py`` and
``agents/planner.py`` call ``core.client.create_client()`` directly
whenever the model is Claude (the fleet default), so the highest-volume
coding path never enters ``providers/`` and a BaseLLMProvider-only hook
would still have leaked it. ``TestSdkClientFamily`` covers that seam.

Coverage:
- TestAdapterInventory      -- every registered adapter is wrapped.
- TestHttpAdapters          -- agentic HTTP payload carries no raw PII.
- TestCliAdapters           -- CLI argv carries no raw PII.
- TestClaudeAdapter         -- prompt forwarded to the SDK is scrubbed.
- TestSdkClientFamily       -- create_client / create_simple_client scrub.
- TestFailClosed            -- a broken redactor refuses to send.
- TestKillSwitch            -- the one escape hatch still works.
- TestCodeIsNotCorrupted    -- code identifiers survive the scrub.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Fake PII. Chosen to hit the built-in high-precision patterns only.
FAKE_SSN = "123-45-6789"
FAKE_EMAIL = "jane.doe@example.com"
FAKE_PHONE = "555-123-4567"
PROMPT = (
    f"Refactor the signup handler. Fixture row: SSN {FAKE_SSN}, "
    f"email {FAKE_EMAIL}, phone {FAKE_PHONE}. "
    "Leave the identifier user_id_123_45_6789 and the constant "
    "MAX_RETRIES_10 alone."
)


def _assert_scrubbed(sent: str) -> None:
    """The wire text must carry the placeholders, never the raw values."""
    for raw in (FAKE_SSN, FAKE_EMAIL, FAKE_PHONE):
        assert raw not in sent, f"raw PII {raw!r} reached the wire: {sent!r}"
    assert "[REDACTED_SSN]" in sent
    assert "[REDACTED_EMAIL]" in sent
    assert "[REDACTED_PHONE]" in sent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default deployment posture: gateway off, kill-switch unset."""
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)


# ---------------------------------------------------------------------------
# 1. Inventory: no adapter may be missing the chokepoint
# ---------------------------------------------------------------------------


def _registered_adapter_classes() -> dict[str, type]:
    """Import every class in both provider registries."""
    import importlib

    from providers.factory import _AGENTIC_REGISTRY, _TEXT_REGISTRY

    classes: dict[str, type] = {}
    for registry in (_AGENTIC_REGISTRY, _TEXT_REGISTRY):
        for module_path, class_name in registry.values():
            try:
                module = importlib.import_module(module_path)
            except ImportError as exc:  # pragma: no cover - env-dependent
                pytest.skip(f"{module_path} not importable: {exc}")
            classes[f"{module_path}.{class_name}"] = getattr(module, class_name)
    return classes


class TestAdapterInventory:
    """Every adapter reachable from the factory routes through the scrub."""

    def test_every_registered_adapter_has_a_wrapped_query(self) -> None:
        unwrapped = [
            name
            for name, cls in _registered_adapter_classes().items()
            if not getattr(cls.query, "__outbound_scrub__", False)
        ]
        assert not unwrapped, (
            f"these adapters send prompts without the outbound PII scrub: {unwrapped}"
        )

    def test_the_inventory_is_not_trivially_empty(self) -> None:
        """Guard the guard: an empty registry would make the test vacuous."""
        assert len(_registered_adapter_classes()) >= 8


# ---------------------------------------------------------------------------
# 2. HTTP adapters -- capture the actual outbound payload
# ---------------------------------------------------------------------------


class TestHttpAdapters:
    """The agentic openai-compatible adapter is the dominant coding path."""

    def test_agentic_payload_carries_no_raw_pii(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from providers.openai_compatible_agentic import (
            OpenAICompatibleAgenticProvider,
        )

        captured: list[dict[str, Any]] = []

        def _fake_post(self: Any, url: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        monkeypatch.setattr(OpenAICompatibleAgenticProvider, "_http_post", _fake_post)

        async def _go() -> None:
            provider = OpenAICompatibleAgenticProvider(
                model="gpt-4o-mini",
                base_url="http://fake.invalid",
                api_key="sk-test",
                working_dir=tmp_path,
            )
            await provider.query(PROMPT)
            async for _ in provider.receive_response():
                pass

        asyncio.run(_go())

        assert captured, "no HTTP call captured -- the probe proved nothing"
        body = json.dumps(captured[0])
        _assert_scrubbed(body)

    def test_ollama_agentic_payload_carries_no_raw_pii(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from providers.ollama_agentic import OllamaAgenticProvider

        provider = OllamaAgenticProvider(
            model="qwen2.5-coder",
            base_url="http://fake.invalid",
            working_dir=tmp_path,
        )
        asyncio.run(provider.query(PROMPT))
        _assert_scrubbed(provider._pending_prompt or "")


# ---------------------------------------------------------------------------
# 3. CLI adapters -- the prompt leaves via argv / stdin, not a JSON body
# ---------------------------------------------------------------------------


class TestCliAdapters:
    """CLI-backed adapters build argv from the stored prompt."""

    def test_copilot_argv_carries_no_raw_pii(self, tmp_path: Path) -> None:
        from providers.copilot_agentic import CopilotAgenticProvider

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query(PROMPT))
        argv = " ".join(provider._build_command())
        _assert_scrubbed(argv)

    @pytest.mark.parametrize(
        "module_path,class_name",
        [
            ("providers.codex_agentic", "CodexAgenticProvider"),
            ("providers.antigravity_agentic", "AntigravityAgenticProvider"),
            ("providers.opencode_agentic", "OpenCodeAgenticProvider"),
            ("providers.codex", "CodexCLIProvider"),
            ("providers.antigravity", "AntigravityCLIProvider"),
        ],
    )
    def test_stored_prompt_is_scrubbed(
        self, module_path: str, class_name: str, tmp_path: Path
    ) -> None:
        """These adapters stash the prompt in ``query`` and shell out later."""
        import importlib

        cls = getattr(importlib.import_module(module_path), class_name)
        provider = cls(working_dir=tmp_path)
        asyncio.run(provider.query(PROMPT))
        _assert_scrubbed(provider._pending_prompt or "")


# ---------------------------------------------------------------------------
# 4. Claude -- forwards straight through to the SDK client
# ---------------------------------------------------------------------------


class TestClaudeAdapter:
    def test_prompt_forwarded_to_the_sdk_is_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from core import client as core_client
        from providers import claude as claude_module

        forwarded: list[str] = []

        class _FakeSDKClient:
            async def query(self, prompt: str) -> None:
                forwarded.append(prompt)

        # ClaudeProvider lazy-imports create_client inside __init__, so the
        # patch has to land on the defining module.
        monkeypatch.setattr(
            core_client, "create_client", lambda **_kwargs: _FakeSDKClient()
        )

        provider = claude_module.ClaudeProvider(
            model="claude-opus-4-7",
            project_dir=tmp_path,
            spec_dir=tmp_path,
        )
        asyncio.run(provider.query(PROMPT))

        assert forwarded, "ClaudeProvider never forwarded the prompt"
        _assert_scrubbed(forwarded[0])


# ---------------------------------------------------------------------------
# 4b. The Claude Agent SDK family -- never enters providers/ at all
# ---------------------------------------------------------------------------


class _RecordingSDKClient:
    """Stand-in for ClaudeSDKClient; records what would go over the wire."""

    def __init__(self, **_kwargs: Any) -> None:
        self.sent: list[str] = []

    async def query(self, prompt: str) -> None:
        self.sent.append(prompt)

    async def __aenter__(self) -> _RecordingSDKClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _stub_sdk_auth(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Neutralise the OAuth lookup both client factories perform.

    CI has no Claude token, and this suite is about what leaves the
    process, not about auth. A placeholder string, never a real
    credential, and it never leaves the test.
    """
    monkeypatch.setattr(module, "require_auth_token", lambda: "not-a-real-token")
    monkeypatch.setattr(module, "get_sdk_env_vars", dict)


class TestSdkClientFamily:
    """agents/coder.py takes create_client() directly for Claude models.

    That path never constructs a BaseLLMProvider, so the provider hook
    alone leaves the dominant coding path unscrubbed.
    """

    def test_create_client_scrubs_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from core import client as core_client

        _stub_sdk_auth(monkeypatch, core_client)
        recorded = _RecordingSDKClient()
        monkeypatch.setattr(
            core_client, "ClaudeSDKClient", lambda **_kw: recorded, raising=False
        )

        wrapped = core_client.create_client(
            project_dir=tmp_path,
            spec_dir=tmp_path,
            model="claude-opus-4-7",
            agent_type="coder",
        )
        asyncio.run(wrapped.query(PROMPT))

        assert recorded.sent, "the SDK client never received a prompt"
        _assert_scrubbed(recorded.sent[0])

    def test_create_simple_client_scrubs_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from core import simple_client as core_simple_client

        _stub_sdk_auth(monkeypatch, core_simple_client)
        recorded = _RecordingSDKClient()
        monkeypatch.setattr(
            core_simple_client,
            "ClaudeSDKClient",
            lambda **_kw: recorded,
            raising=False,
        )

        wrapped = core_simple_client.create_simple_client(
            model="claude-haiku-4-5",
            agent_type="commit_message",
            cwd=tmp_path,
        )
        asyncio.run(wrapped.query(PROMPT))

        assert recorded.sent, "the SDK client never received a prompt"
        _assert_scrubbed(recorded.sent[0])

    def test_the_proxy_is_otherwise_transparent(self) -> None:
        """Everything but query() must pass straight through."""
        from core.outbound_scrub import wrap_client_outbound_scrub

        class _Inner:
            marker = "inner-attribute"

            def receive_response(self) -> str:
                return "stream"

        wrapped = wrap_client_outbound_scrub(_Inner())
        assert wrapped.marker == "inner-attribute"
        assert wrapped.receive_response() == "stream"

    def test_wrapping_twice_is_a_no_op(self) -> None:
        from core.outbound_scrub import wrap_client_outbound_scrub

        once = wrap_client_outbound_scrub(_RecordingSDKClient())
        assert wrap_client_outbound_scrub(once) is once


# ---------------------------------------------------------------------------
# 5. Fail closed -- #1010's contract must hold on the new seam too
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_broken_redactor_refuses_to_send_on_a_cli_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A redaction pass that raises must abort, not leak (#320)."""
        from providers.copilot_agentic import CopilotAgenticProvider
        from services import llm_pii_redactor

        class _ExplodingPattern:
            pattern = "<exploding>"

            def sub(self, *_args: Any, **_kwargs: Any) -> str:
                raise re.error("simulated pathological backtrack")

        monkeypatch.setattr(
            llm_pii_redactor,
            "_BUILTIN_PATTERNS",
            [(_ExplodingPattern(), "[REDACTED]")],
        )

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        with pytest.raises(RuntimeError, match="refusing to send"):
            asyncio.run(provider.query(PROMPT))

        assert provider._pending_prompt is None, (
            "the prompt must not be stored when the scrub failed closed"
        )

    def test_missing_redactor_refuses_to_send(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No redactor on PYTHONPATH must not degrade to sending raw."""
        from providers import BaseLLMProvider
        from providers.copilot_agentic import CopilotAgenticProvider

        def _boom(self: Any) -> Any:
            raise ImportError("redactor module not on PYTHONPATH")

        monkeypatch.setattr(BaseLLMProvider, "_build_outbound_redactor", _boom)

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        with pytest.raises(RuntimeError, match="refusing to send"):
            asyncio.run(provider.query(PROMPT))


# ---------------------------------------------------------------------------
# 6. Kill-switch -- exactly one escape hatch, and it still works
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_env_false_restores_raw_prompts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "false")

        from providers.copilot_agentic import CopilotAgenticProvider

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query(PROMPT))
        assert FAKE_SSN in (provider._pending_prompt or "")

    def test_unset_env_means_scrub_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from providers.copilot_agentic import CopilotAgenticProvider

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query(PROMPT))
        assert FAKE_SSN not in (provider._pending_prompt or "")


# ---------------------------------------------------------------------------
# 7. This is a code factory -- the scrub must not corrupt code
# ---------------------------------------------------------------------------


class TestCodeIsNotCorrupted:
    def test_code_identifiers_survive_the_scrub(self, tmp_path: Path) -> None:
        """Underscored / bare identifiers are not SSN-shaped, so they stay."""
        from providers.copilot_agentic import CopilotAgenticProvider

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query(PROMPT))
        sent = provider._pending_prompt or ""
        assert "user_id_123_45_6789" in sent
        assert "MAX_RETRIES_10" in sent

    def test_semver_uuid_and_cidr_are_untouched(self, tmp_path: Path) -> None:
        """The shapes a code prompt is actually full of."""
        from providers.copilot_agentic import CopilotAgenticProvider

        code_prompt = (
            "Bump to 1.2.3-rc.4, keep uuid "
            "550e8400-e29b-41d4-a716-446655440000, allow 10.0.0.0/8, "
            "sha 0a1b2c3d4e5f6a7b, port 8080, and the hex 0xdeadbeef."
        )
        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query(code_prompt))
        assert provider._pending_prompt == code_prompt


# ---------------------------------------------------------------------------
# 8. Audit semantics from #1010 survive the move to the base class
# ---------------------------------------------------------------------------


class TestAuditSemantics:
    def test_raw_prompt_and_flag_are_recorded_for_the_audit_row(
        self, tmp_path: Path
    ) -> None:
        from providers.copilot_agentic import CopilotAgenticProvider

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query(PROMPT))
        assert provider._outbound_prompt_raw == PROMPT
        assert provider._outbound_prompt_scrubbed is True

    def test_flag_is_false_when_nothing_matched(self, tmp_path: Path) -> None:
        from providers.copilot_agentic import CopilotAgenticProvider

        provider = CopilotAgenticProvider(working_dir=tmp_path)
        asyncio.run(provider.query("Add a retry to the client."))
        assert provider._outbound_prompt_scrubbed is False
