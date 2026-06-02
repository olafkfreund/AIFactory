"""
OpenCodeAgenticProvider — OpenCode CLI runtime adapter for coding/planning phases
=================================================================================

Runs the OpenCode CLI (``opencode``) in its non-interactive ``run`` mode::

    opencode run --model <provider/model> -p <prompt>

OpenCode is a CLI coding-agent runtime (similar in shape to the Codex and
Gemini CLI agentic providers).  The ``run`` subcommand is already
non-interactive and autonomous — it reads/writes files and executes commands
in the working directory without an interactive approval loop, so there is no
separate ``--yolo`` / ``--allow-all-tools`` flag to pass (unlike Gemini /
Copilot).

Model routing
-------------
OpenCode's native ``--model`` flag expects a ``provider/model`` string
(e.g. ``anthropic/claude-sonnet-4-5``, ``opencode/sonic``).  AIFactory selects
this runtime with an ``opencode:`` model prefix, e.g.::

    opencode:opencode/sonic
    opencode:anthropic/claude-sonnet-4-5

The ``opencode:`` prefix selects the *provider* (this class); it is stripped
before the remaining ``provider/model`` string is handed to the CLI's
``--model`` flag.  When no model is given, AIFactory defaults to
``opencode/sonic`` — OpenCode's free, no-auth "zen" model — so the runtime
works out of the box without configuring any external API key.

Usage::

    from providers.opencode_agentic import OpenCodeAgenticProvider

    provider = OpenCodeAgenticProvider(
        model="opencode:opencode/sonic",
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
import re
import shutil
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

from providers import BaseLLMProvider
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)

_DEFAULT_OPENCODE_PATH: str = "opencode"
# Free, no-auth "zen" model bundled with OpenCode — works without any API key.
_DEFAULT_MODEL: str = "opencode/sonic"
_DEFAULT_TIMEOUT: int = 600  # 10 minutes for agentic tasks
# OpenCode model IDs are "provider/model" — allow alphanumerics plus . _ : / -
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")


def _strip_opencode_prefix(model: str) -> str:
    """Drop a leading ``opencode:`` from the model string.

    AIFactory routes the OpenCode runtime with ``opencode:<provider/model>`` so
    the prefix selects the provider; the CLI's ``--model`` flag itself wants the
    bare ``provider/model`` form (e.g. ``opencode/sonic``,
    ``anthropic/claude-sonnet-4-5``).
    """
    if model.lower().startswith("opencode:"):
        return model[len("opencode:") :]
    return model


class OpenCodeAgenticProvider(BaseLLMProvider):
    """
    Agentic OpenCode CLI provider for coding/planning/spec/qa phases.

    Runs ``opencode run --model <provider/model> -p <prompt>`` which executes a
    fully autonomous coding session (file ops, commands) in ``working_dir`` and
    returns the agent's final output.  The result is wrapped in a single
    ``AssistantMessage``/``TextBlock`` to satisfy the message protocol consumed
    by ``reviewer.py`` / ``fixer.py``.

    Args:
        model: OpenCode model string, optionally ``opencode:``-prefixed
            (e.g. ``"opencode:opencode/sonic"``).  The bare ``provider/model``
            form is passed to the CLI's ``--model`` flag.  Defaults to
            ``opencode/sonic`` (free, no-auth).
        opencode_path: Path or command name for the ``opencode`` executable.
        timeout: Maximum seconds to wait for the subprocess.
        working_dir: Working directory for the subprocess (OpenCode operates on
            files relative to this directory).
        extra_args: Additional CLI flags appended to the ``run`` invocation.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        opencode_path: str = _DEFAULT_OPENCODE_PATH,
        timeout: int = _DEFAULT_TIMEOUT,
        working_dir: Path | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        if model and not _MODEL_NAME_RE.match(model):
            raise ValueError(
                f"Invalid model name '{model}': must be alphanumeric with . _ : / - separators"
            )
        resolved_model = _strip_opencode_prefix(model).strip() if model else ""
        self._model = resolved_model or _DEFAULT_MODEL
        self._opencode_path = opencode_path
        self._timeout = timeout
        self._working_dir = working_dir
        self._extra_args: list[str] = extra_args or []
        for arg in self._extra_args:
            if "\x00" in arg:
                raise ValueError("extra_args must not contain null bytes")
        self._pending_prompt: str | None = None

        logger.debug(
            "OpenCodeAgenticProvider created model=%s working_dir=%s timeout=%d",
            self._model,
            working_dir,
            timeout,
        )

    async def query(self, prompt: str) -> None:
        """Store the prompt for execution when ``receive_response()`` is called."""
        self._pending_prompt = prompt

    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async generator that runs the OpenCode CLI."""
        return self._run_opencode()

    def _build_command(self) -> list[str]:
        """Build the argv list for the non-interactive OpenCode ``run`` call.

        Shape::

            opencode run --model <provider/model> -p <prompt> [extra_args...]

        The prompt is passed via ``-p`` (rather than stdin / positional message)
        to avoid shell-quoting issues with multi-kilobyte prompt strings.
        """
        cmd: list[str] = [self._opencode_path, "run"]
        if self._model:
            cmd += ["--model", self._model]
        cmd += ["-p", self._pending_prompt or ""]
        if self._extra_args:
            cmd.extend(self._extra_args)
        return cmd

    async def _run_opencode(self) -> AsyncGenerator[Any, None]:
        """Spawn the OpenCode CLI, return its final output as a message block."""
        if not self._pending_prompt:
            logger.warning(
                "OpenCodeAgenticProvider.receive_response() called before query()"
            )
            return

        resolved_path = (
            self._opencode_path
            if self._opencode_path.startswith("/")
            else shutil.which(self._opencode_path)
        )
        if resolved_path is None or (
            self._opencode_path.startswith("/")
            and not Path(self._opencode_path).exists()
        ):
            raise RuntimeError(
                f"OpenCode CLI executable not found: '{self._opencode_path}'. "
                "Install the OpenCode CLI (https://opencode.ai) or pass the "
                "correct path via opencode_path=..."
            )

        cmd = self._build_command()
        cwd = str(self._working_dir) if self._working_dir else None
        logger.debug(
            "OpenCodeAgenticProvider: spawning model=%s cwd=%r", self._model, cwd
        )

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
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
                f"OpenCode CLI timed out after {self._timeout}s."
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        logger.debug(
            "OpenCodeAgenticProvider: finished returncode=%d stdout_len=%d stderr_len=%d",
            proc.returncode,
            len(stdout_text),
            len(stderr_text),
        )

        if proc.returncode != 0 and not stdout_text:
            error_detail = stderr_text or f"exit code {proc.returncode}"
            raise RuntimeError(f"OpenCode CLI error: {error_detail}")

        if stderr_text:
            logger.warning("OpenCode CLI stderr (first 500 chars): %s", stderr_text[:500])

        response_text = stdout_text if stdout_text else "(no output from OpenCode CLI)"
        yield AssistantMessage(content=[TextBlock(text=response_text)])

    async def __aenter__(self) -> OpenCodeAgenticProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._pending_prompt = None


__all__ = ["OpenCodeAgenticProvider"]
