"""
CodexAgenticProvider — MCP-based Codex adapter for agentic phases
==================================================================

Uses ``codex mcp-server`` (stdio JSON-RPC) instead of ``codex exec``.
The MCP server provides full agentic capability: file creation, command
execution, sandbox control, and multi-turn conversations via threadId.

The server is started once in ``__aenter__`` and reused for all calls
within the ``async with`` block.  Communication follows the MCP protocol
(JSON-RPC 2.0 over stdio, one message per line).

Usage::

    from providers.codex_agentic import CodexAgenticProvider

    provider = CodexAgenticProvider(
        model="gpt-5.3-codex",
        working_dir=spec_dir,
        timeout=600,
    )
    async with provider:
        await provider.query(prompt)
        async for msg in provider.receive_response():
            ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

from providers import BaseLLMProvider
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)

# Default to the codex account default (no explicit model) rather than a
# concrete model id.  A ChatGPT-account codex login rejects ANY explicit
# `--model` ("...not supported when using Codex with a ChatGPT account",
# HTTP 400) — only codex's implicit default works for those accounts.  An
# API-key account can still pass a real model id explicitly (e.g.
# "gpt-5.3-codex"), which is forwarded unchanged.  (#293)
_DEFAULT_CODEX_PATH: str = "codex"
_DEFAULT_MODEL: str = ""  # empty => use codex's account default (no model sent)
_DEFAULT_TIMEOUT: int = 600  # 10 minutes for agentic tasks
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")

# Model strings that mean "let codex use the authenticated account's default
# model" — i.e. do NOT send a `model` field in the MCP tool call.  These are
# the values a ChatGPT-account user should select so codex picks its own
# implicit default.  Matched case-insensitively after stripping whitespace.
_ACCOUNT_DEFAULT_MODELS: frozenset[str] = frozenset(
    {"", "codex", "codex:default", "codex-default", "default"}
)


def _is_account_default(model: str | None) -> bool:
    """True when ``model`` means "use codex's account default" (omit model).

    A ChatGPT-account codex login rejects every explicit model, so the codex
    provider must send NO ``model`` field for these sentinel values.  An
    explicit model id (e.g. ``gpt-5.3-codex``) returns False and is forwarded
    unchanged so API-key accounts keep working.  (#293)
    """
    if model is None:
        return True
    return model.strip().lower() in _ACCOUNT_DEFAULT_MODELS

# MCP protocol constants
_MCP_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "aifactory", "version": "1.0"}


class CodexAgenticProvider(BaseLLMProvider):
    """
    Agentic Codex provider using ``codex mcp-server`` (stdio JSON-RPC).

    Starts a persistent MCP server subprocess on enter, sends tool calls
    to run Codex sessions with full agentic capability, and shuts down
    on exit.

    Args:
        model: Codex model identifier (e.g. ``"gpt-5.3-codex"``).
        codex_path: Path or command name for the ``codex`` executable.
        timeout: Maximum seconds to wait for a response.
        working_dir: Working directory for Codex sessions.
        extra_args: Additional CLI flags (unused in MCP mode, kept for API compat).
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        codex_path: str = _DEFAULT_CODEX_PATH,
        timeout: int = _DEFAULT_TIMEOUT,
        working_dir: Path | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        # Normalise the "account default" sentinels ("", "codex",
        # "codex:default", ...) to an empty string so the model field is never
        # sent in the MCP request — required for ChatGPT-account codex logins
        # which reject any explicit model.  Concrete model ids are validated and
        # kept verbatim so API-key accounts still get their chosen model.  (#293)
        if _is_account_default(model):
            self._model = ""
        else:
            if not _MODEL_NAME_RE.match(model):
                raise ValueError(
                    f"Invalid model name '{model}': must be alphanumeric with . _ : / - separators"
                )
            self._model = model
        self._codex_path = codex_path
        self._timeout = timeout
        self._working_dir = working_dir
        self._extra_args: list[str] = extra_args or []
        self._pending_prompt: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._thread_id: str | None = None

        logger.debug(
            "CodexAgenticProvider created model=%s working_dir=%s timeout=%d",
            model,
            working_dir,
            timeout,
        )

    async def _send_message(self, message: dict) -> None:
        """Send a JSON-RPC message to the MCP server via stdin."""
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("MCP server not running")
        line = json.dumps(message) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_response(self, expected_id: int) -> dict:
        """Read a JSON-RPC response from the MCP server stdout.

        Skips notification messages (no 'id' field) and waits for
        the response matching the expected request ID.
        """
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("MCP server not running")

        while True:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=float(self._timeout),
            )
            if not line:
                raise RuntimeError("MCP server closed stdout unexpectedly")

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("CodexMCP: skipping non-JSON line: %s", text[:200])
                continue

            # Skip notifications (no id field)
            if "id" not in data:
                continue

            if data.get("id") == expected_id:
                if "error" in data:
                    error = data["error"]
                    raise RuntimeError(
                        f"MCP error {error.get('code', '?')}: {error.get('message', 'unknown')}"
                    )
                return data

    def _next_id(self) -> int:
        """Get the next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id

    async def __aenter__(self) -> CodexAgenticProvider:
        """Start the MCP server and send initialize handshake."""
        # "codex" is often a shell alias (e.g. -> codex-cli), which shutil.which
        # cannot resolve for create_subprocess_exec. Fall back to the real
        # binary name so agentic Codex works in those environments.
        resolved_path = shutil.which(self._codex_path) or shutil.which("codex-cli")
        if resolved_path is None:
            raise RuntimeError(
                f"Codex CLI executable not found: '{self._codex_path}' "
                "(also tried 'codex-cli'). Install the Codex CLI or pass the correct path."
            )

        cmd = [resolved_path, "mcp-server"]
        logger.info("CodexAgenticProvider: starting MCP server: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stderr concurrently. The response read loop only consumes
        # stdout; during a long agentic coding session codex emits enough
        # stderr to fill the OS pipe buffer (~64 KB), at which point codex
        # BLOCKS on its stderr write and never returns a result on stdout —
        # the build then sees "(no output from Codex MCP)" and writes no
        # files. Keep a small tail for error reporting.
        self._stderr_tail: list[str] = []

        async def _drain_stderr() -> None:
            if not self._proc or not self._proc.stderr:
                return
            try:
                async for raw in self._proc.stderr:
                    self._stderr_tail.append(raw.decode("utf-8", "replace"))
                    if len(self._stderr_tail) > 50:
                        del self._stderr_tail[0]
            except Exception:  # pragma: no cover — best-effort drain
                pass

        self._stderr_task = asyncio.create_task(_drain_stderr())

        # Send initialize
        init_id = self._next_id()
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            }
        )

        response = await self._read_response(init_id)
        server_info = response.get("result", {}).get("serverInfo", {})
        logger.info(
            "CodexAgenticProvider: MCP server initialized — %s v%s",
            server_info.get("name", "unknown"),
            server_info.get("version", "?"),
        )

        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Shut down the MCP server subprocess."""
        self._pending_prompt = None
        self._thread_id = None

        stderr_task = getattr(self, "_stderr_task", None)
        if stderr_task is not None:
            stderr_task.cancel()

        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            finally:
                self._proc = None
                logger.debug("CodexAgenticProvider: MCP server stopped")

    async def query(self, prompt: str) -> None:
        """Store the prompt for execution when ``receive_response()`` is called."""
        self._pending_prompt = prompt

    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async generator that calls the Codex MCP tool."""
        return self._run_codex_mcp()

    async def _run_codex_mcp(self) -> AsyncGenerator[Any, None]:
        """Call the 'codex' tool via MCP and yield the response."""
        if not self._pending_prompt:
            logger.warning(
                "CodexAgenticProvider.receive_response() called before query()"
            )
            return

        if not self._proc:
            raise RuntimeError(
                "MCP server not running — use 'async with' context manager"
            )

        # Build tool call arguments
        arguments: dict[str, Any] = {
            "prompt": self._pending_prompt,
            "approval-policy": "never",
            "sandbox": "danger-full-access",
        }

        if self._model:
            arguments["model"] = self._model

        if self._working_dir:
            arguments["cwd"] = str(self._working_dir)

        # Use codex-reply for multi-turn if we have a thread ID
        tool_name = "codex"
        if self._thread_id:
            tool_name = "codex-reply"
            arguments["threadId"] = self._thread_id

        call_id = self._next_id()
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }
        )

        logger.info(
            "CodexAgenticProvider: sent %s call (id=%d, model=%s, cwd=%s)",
            tool_name,
            call_id,
            self._model,
            self._working_dir,
        )

        response = await self._read_response(call_id)

        # Extract response text
        result = response.get("result", {})
        content_blocks = result.get("content", [])
        structured = result.get("structuredContent", {})

        # Store thread ID for potential multi-turn
        thread_id = structured.get("threadId")
        if thread_id:
            self._thread_id = thread_id

        # Extract text from content blocks
        response_text = ""
        for block in content_blocks:
            if block.get("type") == "text":
                response_text += block.get("text", "")

        if not response_text:
            response_text = structured.get("content", "(no output from Codex MCP)")

        logger.info(
            "CodexAgenticProvider: response received (len=%d, threadId=%s)",
            len(response_text),
            thread_id or "none",
        )

        # Detect error JSON returned as content text by the Codex MCP server.
        # When the model is not available for the authenticated account (e.g.
        # "gpt-5.3-codex" requires an OpenAI API key, not a ChatGPT account),
        # the MCP tool returns a JSON payload with "type":"error" as the sole
        # content block instead of raising an MCP-level error response.  If we
        # yield this as an AssistantMessage the coder loop treats the turn as a
        # successful text exchange, the subtask stays "pending" or "in_progress",
        # no files are written, and every subtask is eventually marked failed
        # after 3 silent retries.
        #
        # Raise a RuntimeError here so the coder loop surfaces the real error
        # message to the operator and counts the turn as an "error" status —
        # not a silent no-op retry.  (#286)
        #
        # NOTE: do not use string-contains to pre-check for '"type":"error"' —
        # JSON serialisers vary in whether they emit spaces after colons, so
        # both '"type":"error"' and '"type": "error"' can appear.  Just parse
        # any response that starts with '{' and check the decoded value.
        _stripped = response_text.strip()
        if _stripped.startswith("{"):
            try:
                _err_payload = json.loads(_stripped)
            except json.JSONDecodeError:
                _err_payload = {}
            if _err_payload.get("type") == "error":
                _err_obj = _err_payload.get("error", {})
                _err_msg = (
                    _err_obj.get("message")
                    if isinstance(_err_obj, dict)
                    else str(_err_obj)
                ) or str(_err_payload)
                _http_status = _err_payload.get("status", "?")
                raise RuntimeError(
                    f"Codex MCP returned an error (HTTP {_http_status}): {_err_msg}"
                )

        yield AssistantMessage(content=[TextBlock(text=response_text)])


__all__ = ["CodexAgenticProvider"]
