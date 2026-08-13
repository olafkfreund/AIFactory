"""Regression guard for the Codex agentic provider (#codex, #286):

1. It must resolve the binary even when 'codex' is only a shell alias
   (fall back to 'codex-cli').
2. It must drain stderr concurrently — otherwise a long coding session fills
   the 64 KB stderr pipe, codex deadlocks, and the build sees
   "(no output from Codex MCP)" with no files written.
3. It must detect error-JSON responses from the Codex MCP tool and raise
   RuntimeError instead of yielding them as agent output.  When the model is
   not available for the authenticated account (e.g. "gpt-5.3-codex" with a
   ChatGPT account), the MCP tool returns {"type":"error","status":400,...} as
   content text.  Without this guard every subtask fails silently after 3
   retries with no files written.  (#286)
"""

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SRC = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "backend"
    / "providers"
    / "codex_agentic.py"
).read_text()


def test_codex_binary_falls_back_to_codex_cli():
    assert 'shutil.which("codex-cli")' in SRC


def test_codex_drains_stderr_concurrently():
    assert "_drain_stderr" in SRC and "create_task" in SRC, (
        "stderr must be drained concurrently to avoid the 64KB pipe deadlock"
    )


def test_codex_error_json_in_content_raises_runtime_error(monkeypatch):
    """Error JSON from Codex MCP must raise RuntimeError, not yield as text.

    When the Codex MCP returns {"type":"error","status":400,"error":{...}} as
    the content-block text (e.g. unsupported model for ChatGPT accounts), the
    provider must raise RuntimeError so the coder loop reports an actual error
    and does not silently exhaust retries.  (#286)
    """
    from providers.codex_agentic import CodexAgenticProvider

    _ERROR_RESPONSE = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"type":"error","status":400,'
                        '"error":{"type":"invalid_request_error",'
                        '"message":"The \'gpt-5.3-codex\' model is not supported '
                        'when using Codex with a ChatGPT account."}}'
                    ),
                }
            ],
            "structuredContent": {},
        },
    }

    async def _fake_send(self, msg):  # noqa: ARG001
        pass

    async def _fake_read(self, expected_id):  # noqa: ARG001
        return _ERROR_RESPONSE

    monkeypatch.setattr(CodexAgenticProvider, "_send_message", _fake_send)
    monkeypatch.setattr(CodexAgenticProvider, "_read_response", _fake_read)

    async def _run():
        p = CodexAgenticProvider(model="gpt-5.3-codex", working_dir=Path("/tmp"))
        # Bypass __aenter__ / MCP startup — just set proc to a truthy sentinel.
        p._proc = object()  # type: ignore[assignment]
        await p.query("implement tictactoe")
        async for _ in p.receive_response():
            pass

    with pytest.raises(RuntimeError, match="ChatGPT account"):
        asyncio.run(_run())


def test_codex_valid_response_does_not_raise(monkeypatch):
    """A normal text response from Codex MCP must be yielded without error."""
    from providers.codex_agentic import CodexAgenticProvider
    from providers.types import AssistantMessage

    _OK_RESPONSE = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "content": [{"type": "text", "text": "hello world"}],
            "structuredContent": {"threadId": "abc-123", "content": "hello world"},
        },
    }

    async def _fake_send(self, msg):  # noqa: ARG001
        pass

    async def _fake_read(self, expected_id):  # noqa: ARG001
        return _OK_RESPONSE

    monkeypatch.setattr(CodexAgenticProvider, "_send_message", _fake_send)
    monkeypatch.setattr(CodexAgenticProvider, "_read_response", _fake_read)

    async def _run():
        p = CodexAgenticProvider(model="gpt-5.3-codex", working_dir=Path("/tmp"))
        p._proc = object()  # type: ignore[assignment]
        await p.query("say hello")
        return [m async for m in p.receive_response()]

    msgs = asyncio.run(_run())
    assert len(msgs) == 1
    assert isinstance(msgs[0], AssistantMessage)
    assert msgs[0].content[0].text == "hello world"


# ---------------------------------------------------------------------------
# Account-default model handling (#293)
#
# A ChatGPT-account codex login rejects ANY explicit model with HTTP 400
# ("not supported when using Codex with a ChatGPT account").  Only codex's
# implicit default (no model sent) works.  The provider must therefore send
# NO `model` field in the MCP tool call when the model string is the
# account-default sentinel ("codex" / "codex:default" / "" / ...), while still
# forwarding a concrete model id for API-key accounts.
# ---------------------------------------------------------------------------

_OK_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 3,
    "result": {
        "content": [{"type": "text", "text": "ok"}],
        "structuredContent": {},
    },
}


def _capture_mcp_arguments(monkeypatch, model):
    """Run the provider with the given model and return the MCP call arguments.

    Mocks the stdio MCP layer (no subprocess, no network): captures the
    tools/call params sent to the codex MCP server and feeds back a canned
    successful response.  Returns the ``arguments`` dict of the tools/call.
    """
    from providers.codex_agentic import CodexAgenticProvider

    captured: dict = {}

    async def _fake_send(self, msg):  # noqa: ARG001
        if msg.get("method") == "tools/call":
            captured["params"] = msg["params"]

    async def _fake_read(self, expected_id):  # noqa: ARG001
        return _OK_RESPONSE

    monkeypatch.setattr(CodexAgenticProvider, "_send_message", _fake_send)
    monkeypatch.setattr(CodexAgenticProvider, "_read_response", _fake_read)

    async def _run():
        p = CodexAgenticProvider(model=model, working_dir=Path("/tmp"))
        p._proc = object()  # type: ignore[assignment]
        await p.query("do something")
        async for _ in p.receive_response():
            pass

    asyncio.run(_run())
    return captured["params"]["arguments"]


@pytest.mark.parametrize("model", ["codex", "codex:default", "default", "", "CODEX"])
def test_codex_account_default_omits_model(monkeypatch, model):
    """Account-default sentinels must NOT send a `model` field (#293)."""
    args = _capture_mcp_arguments(monkeypatch, model)
    assert "model" not in args, (
        f"model {model!r} must resolve to codex account default "
        f"(no `model` in MCP request), got {args!r}"
    )
    # Still a well-formed codex tool call.
    assert args["prompt"] == "do something"
    assert args["sandbox"] == "danger-full-access"


@pytest.mark.parametrize("model", ["gpt-5.3-codex", "gpt-5-codex", "o4-mini"])
def test_codex_explicit_model_is_passed(monkeypatch, model):
    """Explicit model ids must still be sent (API-key-account path) (#293)."""
    args = _capture_mcp_arguments(monkeypatch, model)
    assert args.get("model") == model


def test_codex_default_constant_is_account_default():
    """The provider's default model must now be the account default (#293)."""
    from providers.codex_agentic import _DEFAULT_MODEL, _is_account_default

    assert _is_account_default(_DEFAULT_MODEL)
    assert _is_account_default(None)
    assert not _is_account_default("gpt-5.3-codex")


def test_bare_codex_routes_to_codex_provider():
    """`infer_provider_from_model` routes account-default strings to codex (#293)."""
    from phase_config import infer_provider_from_model

    for m in ("codex", "codex:default", "default", "gpt-5.3-codex"):
        assert infer_provider_from_model(m) == "codex", m


def test_looks_like_codex_error_detects_auth_and_http_failures():
    """#779: an EMPTY codex response paired with an auth/model/http stderr
    marker is a hard failure to surface, not a silent no-op."""
    from providers.codex_agentic import _looks_like_codex_error

    for tail in (
        "Error: 401 Unauthorized",
        "model is not supported when using Codex with a ChatGPT account",
        "HTTP 400 Bad Request",
        "invalid_api_key: the provided key is not valid",
        "you have hit your rate limit",
        "quota exceeded for this account",
    ):
        assert _looks_like_codex_error(tail), tail


def test_looks_like_codex_error_ignores_benign_stderr():
    """Benign progress chatter must NOT be flagged as an error (avoid
    false-failing a legitimate silent-edit codex turn)."""
    from providers.codex_agentic import _looks_like_codex_error

    for tail in (
        "",
        "applying patch to main.py",
        "codex: thinking...",
        "wrote 3 files, 0 warnings",
    ):
        assert not _looks_like_codex_error(tail), tail


def test_empty_response_surfaces_stderr_and_raises_on_error():
    """The empty-response branch must consult the stderr tail and raise when it
    looks like a codex failure (source-level guard for #779)."""
    assert "_looks_like_codex_error(stderr_tail)" in SRC
    assert "_stderr_tail" in SRC and "empty response" in SRC
