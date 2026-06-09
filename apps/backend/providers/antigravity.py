"""
AntigravityCLIProvider — Adapter wrapping the Antigravity CLI
==============================================================

Implements ``BaseLLMProvider`` by spawning the ``antigravity`` command-line
tool (Google's post-Gemini-CLI successor) as a subprocess, sending the QA
prompt via stdin, and wrapping the plain-text response in the
message-protocol types expected by ``reviewer.py`` and ``fixer.py``.

Since the Antigravity CLI is a text-in / text-out interface it does not
natively support the agentic tool-use loop (file reads, writes, etc.).  The
adapter therefore sends the **complete prompt** (including all context already
embedded by the QA prompt builder) and returns the LLM's text as a single
``AssistantMessage`` containing one ``TextBlock``.  The QA reviewer logic
in ``reviewer.py`` still reads the ``qa_signoff`` status from
``implementation_plan.json``, but it will receive the model's analysis as
pure text with no tool calls in the stream.

Back-compat: legacy ``gemini-*`` model IDs still route here, and the old
import path ``providers.gemini`` / ``qa.providers.gemini`` still resolves
``GeminiCLIProvider`` (a shim alias for ``AntigravityCLIProvider``).

Usage::

    from pathlib import Path
    from providers.antigravity import AntigravityCLIProvider

    provider = AntigravityCLIProvider(
        model="gemini-3.1-pro-preview",  # model passed via --model
        antigravity_path="antigravity",  # executable name / absolute path
        timeout=300,                     # subprocess timeout in seconds
        working_dir=project_dir,         # cwd for the subprocess
    )
    async with provider:
        await provider.query(prompt)
        async for msg in provider.receive_response():
            ...

CLI invocation shape::

    antigravity [--model <model>] [<extra_args>...]

The prompt is piped via stdin to avoid shell quoting issues with
multi-kilobyte prompt strings.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import warnings
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

from providers import BaseLLMProvider
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini CLI sunset notice (Issue #22)
# ---------------------------------------------------------------------------
#
# Google announced on 2026-05-19 that the legacy Gemini CLI is sunsetting on
# 2026-06-18 for free / Pro / Ultra personal-tier users. The migration path
# is the Antigravity CLI, which AIFactory now drives directly (this provider).
# Enterprise tier remains supported with their existing API key.
#
# This module still emits a DeprecationWarning the first time a provider is
# instantiated so users on the legacy `gemini` binary see the timeline and
# migrate to the `antigravity` binary before June 18.

_DEPRECATION_MESSAGE = (
    "The legacy Gemini CLI is sunsetting on 2026-06-18 for free / Pro / "
    "Ultra personal-tier users (enterprise tier unaffected). AIFactory now "
    "drives the Antigravity CLI (the `antigravity` binary) directly. If you "
    "are still on the legacy `gemini` binary, install/update the Antigravity "
    "CLI (npm @google/gemini-cli into ~/.gemini/antigravity-cli). Migration "
    "guide: https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/"
)
_warned_sunset = False


def _emit_sunset_warning() -> None:
    """Emit the Gemini CLI sunset DeprecationWarning at most once per process."""
    global _warned_sunset
    if _warned_sunset:
        return
    _warned_sunset = True
    warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=3)
    logger.warning("Gemini CLI sunset: %s", _DEPRECATION_MESSAGE)


# ---------------------------------------------------------------------------
# Module-level defaults (overridable per-instance)
# ---------------------------------------------------------------------------

_DEFAULT_ANTIGRAVITY_PATH: str = "antigravity"
# Newest model validated on the Antigravity/Gemini CLI (2026-06-09).
_DEFAULT_MODEL: str = "gemini-3.5-flash"
_DEFAULT_TIMEOUT: int = 300  # seconds

# Bare provider selectors that mean "this provider", not a literal model —
# passing them as `--model antigravity` → ModelNotFoundError. Map to default.
_PROVIDER_SELECTORS: frozenset[str] = frozenset(
    {"", "antigravity", "antigravity-default", "default"}
)


def _resolve_model(model: str | None) -> str:
    """Map a bare provider selector / ``antigravity:<id>`` to a concrete model."""
    if not model:
        return _DEFAULT_MODEL
    m = model.strip()
    if m.startswith("antigravity:"):
        m = m[len("antigravity:"):].strip()
    return _DEFAULT_MODEL if m in _PROVIDER_SELECTORS else m


def get_antigravity_binary(custom_path: str | None = None) -> str:
    """Dynamically resolve the antigravity / gemini binary path.

    Prefers the ``antigravity`` binary, then the bundled
    ``~/.gemini/antigravity-cli/bin/antigravity`` install location, then the
    legacy ``gemini`` binary, falling back to ``antigravity`` (preinstalled by
    default).
    """
    if custom_path and custom_path not in ("antigravity", "gemini"):
        return custom_path
    if shutil.which("antigravity"):
        return "antigravity"
    from pathlib import Path
    custom_path_default = Path.home() / ".gemini" / "antigravity-cli" / "bin" / "antigravity"
    if custom_path_default.exists():
        return str(custom_path_default)
    if shutil.which("gemini"):
        return "gemini"
    # Fallback to antigravity since we preinstall it by default
    return "antigravity"


# Back-compat alias for the old helper name.
get_gemini_binary = get_antigravity_binary


class AntigravityCLIProvider(BaseLLMProvider):
    """
    QA LLM provider backed by the Antigravity CLI (``antigravity`` subprocess).

    The adapter invokes the ``antigravity`` executable as a subprocess, pipes
    the QA prompt via *stdin*, waits for the process to finish, then yields the
    captured stdout as a single ``AssistantMessage`` containing one
    ``TextBlock``.

    Because the Antigravity CLI does not expose a tool-use protocol, no
    ``ToolUseBlock`` or ``UserMessage`` objects are ever produced.  The QA
    reviewer will receive the model's complete analysis as plain text.

    Args:
        model: Model identifier to request (e.g.
               ``"gemini-3.1-pro-preview"``, ``"gemini-2.5-pro"``).  Passed to
               the CLI via ``--model``.  Set to ``None`` or ``""`` to omit
               the flag and let the CLI use its own default.
        antigravity_path: Path or command name for the ``antigravity``
                     executable.  Resolved via ``$PATH`` when not an absolute
                     path.  Defaults to ``"antigravity"``.
        timeout: Maximum number of seconds to wait for the subprocess to
                 complete before raising ``asyncio.TimeoutError``.
                 Defaults to 300 (5 minutes).
        working_dir: Working directory passed to the subprocess via ``cwd``.
                     Defaults to the process's current directory when
                     ``None``.
        extra_args: Additional CLI flags appended after the model flag
                    (e.g. ``["--no-cache"]``).
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        antigravity_path: str = _DEFAULT_ANTIGRAVITY_PATH,
        timeout: int = _DEFAULT_TIMEOUT,
        working_dir: Path | None = None,
        extra_args: list[str] | None = None,
        gemini_path: str | None = None,  # back-compat alias for antigravity_path
    ) -> None:
        _emit_sunset_warning()  # Issue #22 — flag the 2026-06-18 sunset.
        # Never pass `--model antigravity` (ModelNotFoundError); map bare
        # selectors to the default model.
        self._model = _resolve_model(model)
        # Honour the legacy ``gemini_path`` kwarg if a caller still passes it.
        if gemini_path is not None and antigravity_path == _DEFAULT_ANTIGRAVITY_PATH:
            antigravity_path = gemini_path
        self._antigravity_path = antigravity_path
        self._timeout = timeout
        self._working_dir = working_dir
        self._extra_args: list[str] = extra_args or []
        self._pending_prompt: str | None = None

        logger.debug(
            "AntigravityCLIProvider created",
            extra={
                "model": model,
                "antigravity_path": antigravity_path,
                "timeout": timeout,
                "working_dir": str(working_dir) if working_dir else None,
            },
        )

    # Back-compat: expose the old attribute name as a read-only alias.
    @property
    def _gemini_path(self) -> str:
        return self._antigravity_path

    # ------------------------------------------------------------------
    # BaseLLMProvider interface
    # ------------------------------------------------------------------

    async def query(self, prompt: str) -> None:
        """Store the prompt for execution when ``receive_response()`` is called.

        Args:
            prompt: The system + user prompt string assembled by the QA
                    prompt builder (may be several kB of text).
        """
        self._pending_prompt = prompt
        logger.debug(
            "AntigravityCLIProvider: prompt stored (length=%d)", len(prompt)
        )

    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async generator that runs the Antigravity CLI subprocess.

        The generator invokes ``antigravity`` with the stored prompt as stdin,
        captures stdout, and yields a single ``AssistantMessage``.

        Returns:
            An ``AsyncGenerator`` that yields one ``AssistantMessage``
            containing one ``TextBlock`` with the full CLI response.

        Raises:
            RuntimeError: If the ``antigravity`` executable cannot be found on
                          ``$PATH``, or if the process exits with a non-zero
                          code *and* produces no stdout.
            asyncio.TimeoutError: If the subprocess exceeds ``self._timeout``
                                  seconds without completing.
        """
        return self._run_antigravity()

    async def _run_antigravity(self) -> AsyncGenerator[Any, None]:
        """Async generator: invoke the Antigravity CLI and yield the response.

        Yields:
            ``AssistantMessage(content=[TextBlock(text=<response>)])``
        """
        if not self._pending_prompt:
            logger.warning(
                "AntigravityCLIProvider.receive_response() called before "
                "query() — no prompt to send"
            )
            return

        # Resolve the executable path early so callers get a clear error
        # message rather than a confusing FileNotFoundError from asyncio.
        resolved_binary = get_antigravity_binary(self._antigravity_path)
        resolved_path = shutil.which(resolved_binary) if not resolved_binary.startswith("/") else resolved_binary
        if resolved_path is None or (resolved_binary.startswith("/") and not Path(resolved_binary).exists()):
            raise RuntimeError(
                f"Antigravity CLI executable not found: '{self._antigravity_path}'. "
                "Install the Antigravity CLI or pass the correct path via "
                "antigravity_path=... when constructing AntigravityCLIProvider."
            )

        cmd = self._build_command()
        cwd = str(self._working_dir) if self._working_dir else None

        logger.debug(
            "AntigravityCLIProvider: spawning subprocess cmd=%r cwd=%r", cmd, cwd
        )

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            prompt_bytes = self._pending_prompt.encode("utf-8")
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt_bytes),
                timeout=float(self._timeout),
            )

        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise asyncio.TimeoutError(
                f"Antigravity CLI subprocess timed out after {self._timeout}s. "
                "Increase timeout= or reduce prompt size."
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        logger.debug(
            "AntigravityCLIProvider: subprocess finished returncode=%d "
            "stdout_len=%d stderr_len=%d",
            proc.returncode,
            len(stdout_text),
            len(stderr_text),
        )

        # A non-zero exit with no stdout is a fatal error.
        if proc.returncode != 0 and not stdout_text:
            error_detail = stderr_text or f"exit code {proc.returncode}"
            raise RuntimeError(
                f"Antigravity CLI exited with an error: {error_detail}"
            )

        # Log stderr as a warning when present but non-fatal.
        if stderr_text:
            logger.warning(
                "Antigravity CLI stderr (first 500 chars): %s",
                stderr_text[:500],
            )

        response_text = (
            stdout_text if stdout_text else "(no output from Antigravity CLI)"
        )

        yield AssistantMessage(content=[TextBlock(text=response_text)])

    def _build_command(self) -> list[str]:
        """Construct the argv list for the Antigravity CLI subprocess.

        The resulting command reads the prompt from *stdin* to avoid shell
        quoting issues with long, multi-line prompt strings.

        Shape::

            antigravity [--model <model>] [extra_args...]

        The prompt is supplied via stdin.

        Returns:
            A list of strings suitable for ``asyncio.create_subprocess_exec``.
        """
        resolved_binary = get_antigravity_binary(self._antigravity_path)
        cmd: list[str] = [resolved_binary]

        if self._model:
            cmd += ["--model", self._model]

        if self._extra_args:
            cmd.extend(self._extra_args)

        return cmd

    # ------------------------------------------------------------------
    # Async context manager — no persistent resources to manage
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AntigravityCLIProvider:
        """No-op context entry — the subprocess is spawned per-request."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """No-op context exit — clear pending prompt for hygiene."""
        self._pending_prompt = None


# Back-compat alias: legacy imports of ``GeminiCLIProvider`` keep working.
GeminiCLIProvider = AntigravityCLIProvider

__all__ = ["AntigravityCLIProvider", "GeminiCLIProvider", "get_antigravity_binary"]
