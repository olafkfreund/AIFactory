"""
CopilotAgenticProvider — GitHub Copilot CLI adapter for coding/planning phases
==============================================================================

Runs the GitHub Copilot CLI (``copilot``) in non-interactive mode::

    copilot -p <prompt> --allow-all-tools --add-dir <dir> --model <model> --no-color

``--allow-all-tools`` is Copilot's equivalent of Gemini's ``--yolo``: it
auto-approves every tool action (file reads/writes, command execution) so the
agent can complete a coding task without interactive confirmation.  The CLI
authenticates with the user's GitHub Copilot subscription, so no API key is
required here.

Copilot is a *router*: its ``--model`` accepts ``claude-sonnet-4.5``,
``claude-sonnet-4`` or ``gpt-5`` and dispatches to that backend.  AIFactory
selects it with a ``copilot:<model>`` model string, e.g.
``copilot:claude-sonnet-4.5`` or ``copilot:gpt-5``.

Usage::

    from providers.copilot_agentic import CopilotAgenticProvider

    provider = CopilotAgenticProvider(
        model="copilot:claude-sonnet-4.5",
        working_dir=project_dir,
        timeout=600,
    )
    async with provider:
        await provider.query(prompt)
        async for msg in provider.receive_response():
            ...
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
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)

_DEFAULT_COPILOT_PATH: str = "copilot"
# Copilot CLI exposes a fixed model menu; default to the strongest available.
_DEFAULT_MODEL: str = "claude-sonnet-4.5"
_KNOWN_MODELS: frozenset[str] = frozenset(
    {"claude-sonnet-4.5", "claude-sonnet-4", "gpt-5"}
)
_DEFAULT_TIMEOUT: int = 600  # 10 minutes for agentic tasks
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")


def _strip_copilot_prefix(model: str) -> str:
    """Drop a leading ``copilot:`` from the model string.

    AIFactory routes Copilot with ``copilot:<backend>`` so the prefix selects
    the provider; the CLI itself only wants the bare backend name
    (``claude-sonnet-4.5`` / ``gpt-5``).
    """
    if model.lower().startswith("copilot:"):
        return model[len("copilot:") :]
    return model


class CopilotAgenticProvider(BaseLLMProvider):
    """
    Agentic GitHub Copilot CLI provider for coding/planning/spec/qa phases.

    Runs ``copilot -p <prompt> --allow-all-tools`` which auto-approves all tool
    actions (file ops, commands) and edits files in ``working_dir``.  Streams
    the CLI's final output back as an ``AssistantMessage``/``TextBlock``.

    Args:
        model: Copilot model string, optionally ``copilot:``-prefixed
            (e.g. ``"copilot:gpt-5"``).  Resolved to one of
            ``claude-sonnet-4.5`` / ``claude-sonnet-4`` / ``gpt-5``.
        copilot_path: Path or command name for the ``copilot`` executable.
        timeout: Maximum seconds to wait for the subprocess.
        working_dir: Working directory for the subprocess (also added to the
            CLI's allowed file-access list via ``--add-dir``).
        extra_args: Additional CLI flags.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        copilot_path: str = _DEFAULT_COPILOT_PATH,
        timeout: int = _DEFAULT_TIMEOUT,
        working_dir: Path | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        if model and not _MODEL_NAME_RE.match(model):
            raise ValueError(
                f"Invalid model name '{model}': must be alphanumeric with . _ : / - separators"
            )
        resolved_model = _strip_copilot_prefix(model).strip() if model else ""
        if resolved_model and resolved_model not in _KNOWN_MODELS:
            logger.warning(
                "CopilotAgenticProvider: model %r is not one of %s; "
                "falling back to %s",
                resolved_model,
                sorted(_KNOWN_MODELS),
                _DEFAULT_MODEL,
            )
            resolved_model = _DEFAULT_MODEL
        self._model = resolved_model or _DEFAULT_MODEL
        self._copilot_path = copilot_path
        self._timeout = timeout
        self._working_dir = working_dir
        self._extra_args: list[str] = extra_args or []
        for arg in self._extra_args:
            if "\x00" in arg:
                raise ValueError("extra_args must not contain null bytes")
        self._pending_prompt: str | None = None

        logger.debug(
            "CopilotAgenticProvider created model=%s working_dir=%s timeout=%d",
            self._model,
            working_dir,
            timeout,
        )

    async def query(self, prompt: str) -> None:
        """Store the prompt for execution when ``receive_response()`` is called."""
        self._pending_prompt = prompt

    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async generator that runs the Copilot CLI."""
        return self._run_copilot()

    def _build_command(self) -> list[str]:
        """Build the argv list for the non-interactive Copilot invocation."""
        resolved_binary = self._copilot_path
        cmd: list[str] = [
            resolved_binary,
            "-p",
            self._pending_prompt or "",
            "--allow-all-tools",
            "--no-color",
        ]
        if self._working_dir:
            cmd += ["--add-dir", str(self._working_dir)]
        if self._model:
            cmd += ["--model", self._model]
        if self._extra_args:
            cmd.extend(self._extra_args)
        return cmd

    async def _run_copilot(self) -> AsyncGenerator[Any, None]:
        """Spawn the Copilot CLI, return its final output as a message block."""
        if not self._pending_prompt:
            logger.warning("CopilotAgenticProvider.receive_response() called before query()")
            return

        resolved_path = (
            self._copilot_path
            if self._copilot_path.startswith("/")
            else shutil.which(self._copilot_path)
        )
        if resolved_path is None or (
            self._copilot_path.startswith("/") and not Path(self._copilot_path).exists()
        ):
            raise RuntimeError(
                f"GitHub Copilot CLI executable not found: '{self._copilot_path}'. "
                "Install the Copilot CLI (npm i -g @github/copilot) and sign in "
                "with a GitHub Copilot subscription."
            )

        cmd = self._build_command()
        cwd = str(self._working_dir) if self._working_dir else None
        logger.debug("CopilotAgenticProvider: spawning model=%s cwd=%r", self._model, cwd)

        proc: asyncio.subprocess.Process | None = None
        try:
            # COPILOT_ALLOW_ALL mirrors --allow-all-tools for non-interactive runs.
            env = {**os.environ, "COPILOT_ALLOW_ALL": "true"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(self._timeout),
            )
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise asyncio.TimeoutError(
                f"GitHub Copilot CLI timed out after {self._timeout}s."
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        logger.debug(
            "CopilotAgenticProvider: finished returncode=%d stdout_len=%d stderr_len=%d",
            proc.returncode,
            len(stdout_text),
            len(stderr_text),
        )

        if proc.returncode != 0 and not stdout_text:
            error_detail = stderr_text or f"exit code {proc.returncode}"
            raise RuntimeError(f"GitHub Copilot CLI error: {error_detail}")

        if stderr_text:
            logger.warning("Copilot CLI stderr (first 500 chars): %s", stderr_text[:500])

        response_text = stdout_text if stdout_text else "(no output from Copilot CLI)"
        yield AssistantMessage(content=[TextBlock(text=response_text)])

    async def __aenter__(self) -> CopilotAgenticProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._pending_prompt = None


__all__ = ["CopilotAgenticProvider"]
