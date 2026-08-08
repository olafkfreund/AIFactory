"""Insights chat must not put PII on the wire (#1132, #1139, Factory#642).

These tests do not inspect the scrubber. They stand up a recording
stand-in for the provider endpoint -- a real local HTTP server for the
OpenAI-compatible strategy, a real executable for the Claude CLI strategy
-- and assert on the bytes the strategy actually sent. An egress control
that has never been observed carrying what it claims to carry is
indistinguishable from one that carries everything (Factory#642).

Every value below is synthetic. ``4111111111111111`` is the standard test
PAN; it is Luhn-valid, so it exercises the credit-card path rather than
the raw-digit-run rejection (#1139). No third-party API is contacted.
"""

from __future__ import annotations

import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

WEB_SERVER = Path(__file__).resolve().parents[1]
# ``core`` lives in apps/backend. The ABC under test puts it on sys.path
# itself, but do it here too so the imports below can sit in sorted order
# rather than depending on one import's side effect.
BACKEND = WEB_SERVER.parent / "backend"
for _root in (WEB_SERVER, BACKEND):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from core import outbound_scrub  # noqa: E402
from server.services.insights_providers import (  # noqa: E402
    claude_provider,
    openai_compat_provider,
)
from server.services.insights_providers.base import (  # noqa: E402
    ProviderInfo,
    ProviderStrategy,
)

# Synthetic PII, one value per built-in class the outbound scrub covers.
# #1139: all four are enumerated rather than counted, because a count
# survives a silent narrowing of the pattern set.
SSN = "123-45-6789"
EMAIL = "jane.doe@example.com"
PHONE = "555-123-4567"
PAN = "4111111111111111"

PII_VALUES = (SSN, EMAIL, PHONE, PAN)
PII_MARKERS = (
    "[REDACTED_SSN]",
    "[REDACTED_EMAIL]",
    "[REDACTED_PHONE]",
    "[REDACTED_CC]",
)

# The non-PII half of a realistic insights question. A scrubber that
# removes these is a working scrubber and a broken product, so they are
# asserted as loudly as the PII is.
CONTEXT = ("services/billing/charge.py", "retry_policy=exponential", "exit code 2")

PROMPT = (
    f"Why did the checkout job fail for customer SSN {SSN}? "
    f"Contact {EMAIL} or {PHONE}. Card on file {PAN}. "
    "The stack trace points at services/billing/charge.py, "
    "retry_policy=exponential, and the runner died with exit code 2."
)

HISTORY = [
    {"role": "user", "content": f"Earlier I gave you card {PAN}"},
    {"role": "assistant", "content": "Understood."},
]


@pytest.fixture(autouse=True)
def _silence_websocket_broadcasts(monkeypatch):
    """The strategies broadcast progress over a WebSocket hub these tests
    do not run. Neutralise it so the assertions are about the wire."""

    async def _noop(*_args, **_kwargs):
        return None

    for module in (openai_compat_provider, claude_provider):
        monkeypatch.setattr(module, "broadcast_event", _noop)


@pytest.fixture(autouse=True)
def _scrub_default_on(monkeypatch):
    """Default-on is the behaviour under test; never inherit the operator
    kill-switch from the ambient environment."""
    monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)


class _Recorder(BaseHTTPRequestHandler):
    """Records the request body, then replays a minimal SSE completion."""

    bodies: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        type(self).bodies.append(self.rfile.read(length).decode())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = json.dumps(
            {"choices": [{"delta": {"content": "Two charges timed out."}}]}
        )
        self.wfile.write(f"data: {chunk}\n\ndata: [DONE]\n\n".encode())

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def recording_endpoint():
    """A real HTTP server standing in for an OpenAI-compatible provider."""
    _Recorder.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _Recorder.bodies
    finally:
        server.shutdown()
        server.server_close()


async def _send_via_openai_compat(base_url: str, *, history=HISTORY) -> str:
    provider = openai_compat_provider.OpenAICompatProvider(
        "lmstudio", base_url=base_url
    )
    return await provider.send_message(
        project_path=Path(),
        project_id="proj-1",
        message=PROMPT,
        model="test-model",
        model_config=None,
        conversation_history=history,
    )


async def test_http_wire_carries_no_pii_and_still_carries_the_question(
    recording_endpoint,
):
    """#1132/#1139: all four built-in classes are absent from the captured
    outbound body, all four redaction markers are present, and the
    question the user actually asked survives."""
    base_url, bodies = recording_endpoint
    reply = await _send_via_openai_compat(base_url)

    assert bodies, "nothing reached the recording endpoint"
    sent = bodies[0]

    for value in PII_VALUES:
        assert value not in sent
    for marker in PII_MARKERS:
        assert marker in sent, f"{marker} never reached the wire"

    # The feature still works: the strategy parsed the stream and returned
    # the assistant's answer for persistence and display.
    assert reply == "Two charges timed out."
    # ... and the model still has something to answer with.
    for probe in CONTEXT:
        assert probe in sent, f"scrub destroyed useful context: {probe}"


async def test_replayed_conversation_history_is_scrubbed_too(recording_endpoint):
    """The stateless strategies replay prior turns on every request.
    Scrubbing only the new message leaks every earlier turn forever."""
    base_url, bodies = recording_endpoint
    await _send_via_openai_compat(base_url)

    payload = json.loads(bodies[0])
    history_turn = payload["messages"][0]["content"]
    assert PAN not in history_turn
    assert "[REDACTED_CC]" in history_turn


async def test_claude_cli_argv_carries_no_pii(tmp_path, monkeypatch):
    """The Claude strategy's wire is subprocess argv, not an HTTP body.
    Stand in a recording executable and read what it was handed."""
    argv_log = tmp_path / "argv.json"
    fake_cli = tmp_path / "claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(argv_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': "
        "[{'type': 'text', 'text': 'Two charges timed out.'}]}}))\n"
    )
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC)

    provider = claude_provider.ClaudeProvider()
    provider._claude_path = str(fake_cli)
    monkeypatch.setattr(
        claude_provider.ClaudeProvider,
        "_resolve_claude_token",
        lambda _self: (None, None, None),
    )

    reply = await provider.send_message(
        project_path=tmp_path,
        project_id="proj-1",
        message=PROMPT,
        model="sonnet",
        model_config=None,
        conversation_history=None,
    )

    wire = " ".join(json.loads(argv_log.read_text()))
    for value in PII_VALUES:
        assert value not in wire
    for marker in PII_MARKERS:
        assert marker in wire, f"{marker} never reached the CLI argv"
    assert reply == "Two charges timed out."


async def test_the_assertions_observe_the_scrub_and_not_the_stand_in(
    recording_endpoint, monkeypatch
):
    """Mutation control: with the operator kill-switch set, the same
    capture must show the raw values. A test that passes either way is
    watching the stand-in, not the scrubber."""
    monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "false")
    base_url, bodies = recording_endpoint
    await _send_via_openai_compat(base_url)

    sent = bodies[0]
    for value in PII_VALUES:
        assert value in sent, "kill-switch did not restore the raw prompt"
    for marker in PII_MARKERS:
        assert marker not in sent


async def test_a_new_strategy_cannot_opt_out_of_the_scrub():
    """The seam, not the call sites (#1132).

    A strategy written after this change -- one that never heard of the
    scrub -- is covered anyway, because ``__init_subclass__`` wraps
    ``send_message`` rather than each provider calling a helper. This is
    the assertion that makes "one new provider away from a bypass" false.
    """
    seen: dict[str, object] = {}

    class BrandNewProvider(ProviderStrategy):
        async def detect(self) -> ProviderInfo:
            return ProviderInfo(
                provider="brand-new",
                available=True,
                display_name="Brand New",
                icon="x",
            )

        # Signature is fixed by the ABC, hence the argument-count and
        # unused-argument waivers: a strategy that narrowed it would not be a
        # strategy.
        async def send_message(  # noqa: PLR0913, PLR0917
            self,
            project_path,  # noqa: ARG002
            project_id,  # noqa: ARG002
            message,
            model,  # noqa: ARG002
            model_config,  # noqa: ARG002
            conversation_history,
        ) -> str:
            seen["message"] = message
            seen["history"] = conversation_history
            return "ok"

    await BrandNewProvider().send_message(
        project_path=Path(),
        project_id="proj-1",
        message=PROMPT,
        model=None,
        model_config=None,
        conversation_history=HISTORY,
    )

    for value in PII_VALUES:
        assert value not in seen["message"]
    assert PAN not in seen["history"][0]["content"]


async def test_fails_closed_when_the_redactor_is_unavailable(
    recording_endpoint, monkeypatch
):
    """#320 contract: a scrub that cannot run must refuse to send, not
    fall through to the raw prompt. The recording endpoint must see
    nothing at all."""

    def _boom() -> object:
        raise ImportError("redactor module not on PYTHONPATH")

    monkeypatch.setattr(outbound_scrub, "build_outbound_redactor", _boom)

    base_url, bodies = recording_endpoint
    with pytest.raises(RuntimeError):
        await _send_via_openai_compat(base_url)

    assert not bodies, "a request reached the provider despite a failed scrub"
