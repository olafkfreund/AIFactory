"""
AntigravityAgenticProvider — Agentic Antigravity CLI adapter for coding/planning
=================================================================================

Runs ``antigravity --yolo`` as a subprocess, which auto-approves all tool
actions (file reads/writes, command execution) autonomously.  The prompt is
sent via stdin and the CLI's output is streamed back as ``AssistantMessage`` /
``TextBlock`` objects.

Unlike ``AntigravityCLIProvider`` (text-only), this provider uses ``--yolo``
mode which gives the CLI full agentic capabilities without requiring Docker.

Back-compat: legacy ``gemini-*`` model IDs still route here, and the old
import path ``providers.gemini_agentic`` still resolves
``GeminiAgenticProvider`` (a shim alias for ``AntigravityAgenticProvider``).

Usage::

    from providers.antigravity_agentic import AntigravityAgenticProvider

    provider = AntigravityAgenticProvider(
        model="gemini-3.1-pro-preview",
        working_dir=project_dir,
        timeout=600,
    )
    async with provider:
        await provider.query(prompt)
        async for msg in provider.receive_response():
            ...

CLI invocation shape::

    antigravity --yolo -p <prompt> [--model <model>] [<extra_args>...]
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

from providers import BaseLLMProvider
from providers.antigravity import _emit_sunset_warning  # Issue #22
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)

_DEFAULT_ANTIGRAVITY_PATH: str = "antigravity"
# Newest model validated on the Antigravity/Gemini CLI for this account
# (2026-06-09: gemini-3.5-flash → OK; gemini-3.5-pro → ModelNotFound).
_DEFAULT_MODEL: str = "gemini-3.5-flash"
_DEFAULT_TIMEOUT: int = 600  # 10 minutes for agentic tasks
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")

# Bare provider-selector strings that mean "use this provider", NOT a literal
# Gemini model. Passing them as `--model antigravity` yields
# `ModelNotFoundError: models/antigravity` and the CLI exits 1 → the build fails
# with no completed subtask. Map these to _DEFAULT_MODEL instead.
_PROVIDER_SELECTORS: frozenset[str] = frozenset(
    {"", "antigravity", "antigravity-default", "default"}
)


def _resolve_model(model: str | None) -> str:
    """Resolve a task model string to a concrete Gemini model id.

    - Bare provider selectors (``antigravity``/``default``/empty) → ``_DEFAULT_MODEL``.
    - ``antigravity:<id>`` → ``<id>`` (e.g. ``antigravity:gemini-3.5-flash``).
    - Concrete ids (``gemini-*``, ``antigravity-*``) pass through unchanged.
    """
    if not model:
        return _DEFAULT_MODEL
    m = model.strip()
    if m.startswith("antigravity:"):
        m = m[len("antigravity:"):].strip()
    if m in _PROVIDER_SELECTORS:
        return _DEFAULT_MODEL
    return m


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

    custom_path_default = (
        Path.home() / ".gemini" / "antigravity-cli" / "bin" / "antigravity"
    )
    if custom_path_default.exists():
        return str(custom_path_default)
    if shutil.which("gemini"):
        return "gemini"
    # Fallback to antigravity since we preinstall it by default
    return "antigravity"


# Back-compat alias for the old helper name.
get_gemini_binary = get_antigravity_binary


class AntigravityAgenticProvider(BaseLLMProvider):
    """
    Agentic Antigravity provider for coding/planning/spec/qa_fixer phases.

    Runs ``antigravity --yolo`` which auto-approves all tool actions (file ops,
    commands) autonomously.  Streams output as AssistantMessage/TextBlock
    messages.

    Args:
        model: Model identifier (e.g. ``"gemini-3.1-pro-preview"``).
        antigravity_path: Path or command name for the ``antigravity``
            executable.
        timeout: Maximum seconds to wait for the subprocess.
        working_dir: Working directory for the subprocess.
        extra_args: Additional CLI flags.
        gemini_path: Deprecated alias for ``antigravity_path`` (back-compat).
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
        # Map bare provider selectors ("antigravity"/"default") to a real model
        # so we never pass `--model antigravity` (ModelNotFoundError → exit 1).
        model = _resolve_model(model)
        if model and not _MODEL_NAME_RE.match(model):
            raise ValueError(
                f"Invalid model name '{model}': must be alphanumeric with . _ : / - separators"
            )
        self._model = model
        # Honour the legacy ``gemini_path`` kwarg if a caller still passes it.
        if gemini_path is not None and antigravity_path == _DEFAULT_ANTIGRAVITY_PATH:
            antigravity_path = gemini_path
        self._antigravity_path = antigravity_path
        self._timeout = timeout
        self._working_dir = working_dir
        self._extra_args: list[str] = extra_args or []
        for arg in self._extra_args:
            if "\x00" in arg:
                raise ValueError("extra_args must not contain null bytes")
        self._pending_prompt: str | None = None

        logger.debug(
            "AntigravityAgenticProvider created model=%s working_dir=%s timeout=%d",
            model,
            working_dir,
            timeout,
        )

    # Back-compat: expose the old attribute name as a read-only alias.
    @property
    def _gemini_path(self) -> str:
        return self._antigravity_path

    async def query(self, prompt: str) -> None:
        """Store the prompt for execution when ``receive_response()`` is called."""
        self._pending_prompt = prompt

    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async generator that runs the Antigravity CLI in yolo mode."""
        return self._run_antigravity()

    async def _run_antigravity(self) -> AsyncGenerator[Any, None]:
        """Spawn antigravity --yolo, stream output as AssistantMessage blocks."""
        if not self._pending_prompt:
            logger.warning(
                "AntigravityAgenticProvider.receive_response() called before query()"
            )
            return

        resolved_binary = get_antigravity_binary(self._antigravity_path)
        resolved_path = (
            shutil.which(resolved_binary)
            if not resolved_binary.startswith("/")
            else resolved_binary
        )
        if resolved_path is None or (
            resolved_binary.startswith("/") and not Path(resolved_binary).exists()
        ):
            raise RuntimeError(
                f"Antigravity CLI executable not found: '{self._antigravity_path}'. "
                "Install the Antigravity CLI or pass the correct path."
            )

        cmd = self._build_command()
        cwd = str(self._working_dir) if self._working_dir else None

        logger.debug("AntigravityAgenticProvider: spawning cmd=%r cwd=%r", cmd, cwd)

        proc: asyncio.subprocess.Process | None = None
        try:
            # The CLI refuses tool calls in an "untrusted" workspace — which
            # our isolated git worktrees always are — silently failing every
            # coding subtask (no files written). Trust the workspace so --yolo
            # can actually edit files. See the CLI's trusted-folders guidance.
            # (Env var name is GEMINI_CLI_TRUST_WORKSPACE — the binary kept the
            # legacy name across the gemini-cli -> antigravity-cli rename.)
            env = {**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
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
                f"Antigravity CLI (yolo) timed out after {self._timeout}s."
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        logger.debug(
            "AntigravityAgenticProvider: finished returncode=%d stdout_len=%d stderr_len=%d",
            proc.returncode,
            len(stdout_text),
            len(stderr_text),
        )

        if proc.returncode != 0:
            # Raise on any non-zero exit, even if the CLI produced some stdout.
            # Previously we only raised when stdout was empty, so a CLI that
            # failed but printed a partial response would silently continue —
            # no files would be written, the subtask would be marked failed
            # with no logged error, and the session appeared to succeed.
            error_detail = stderr_text or stdout_text or f"exit code {proc.returncode}"
            raise RuntimeError(
                f"Antigravity CLI (yolo) exited {proc.returncode}: {error_detail[:500]}"
            )

        if stderr_text:
            logger.warning(
                "Antigravity CLI stderr (first 500 chars): %s", stderr_text[:500]
            )

        response_text = (
            stdout_text if stdout_text else "(no output from Antigravity CLI)"
        )

        yield AssistantMessage(content=[TextBlock(text=response_text)])

    def _build_command(self) -> list[str]:
        """Build the argv list for ``antigravity --yolo -p <prompt>``."""
        resolved_binary = get_antigravity_binary(self._antigravity_path)
        cmd: list[str] = [resolved_binary, "--yolo"]

        if self._model:
            cmd += ["--model", self._model]

        if self._extra_args:
            cmd.extend(self._extra_args)

        return cmd

    async def __aenter__(self) -> AntigravityAgenticProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._pending_prompt = None


# Back-compat alias: legacy imports of ``GeminiAgenticProvider`` keep working.
GeminiAgenticProvider = AntigravityAgenticProvider

__all__ = [
    "AntigravityAgenticProvider",
    "GeminiAgenticProvider",
    "get_antigravity_binary",
]
