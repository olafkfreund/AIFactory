"""
OpenCodeAgenticProvider — OpenCode CLI runtime adapter for coding/planning phases
=================================================================================

Runs the OpenCode CLI (``opencode``) in its non-interactive ``run`` mode::

    opencode run --model <provider/model> <prompt>

OpenCode is a CLI coding-agent runtime (similar in shape to the Codex and
Gemini CLI agentic providers).  The ``run`` subcommand is already
non-interactive and autonomous — it reads/writes files and executes commands
in the working directory without an interactive approval loop, so there is no
separate ``--yolo`` / ``--allow-all-tools`` flag to pass (unlike Gemini /
Copilot).

Support tier
------------
OpenCode is a **community / self-host tier** provider.  It is NOT
enterprise-certified: the model catalogue is resolved from the remote
``models.dev`` registry, so individual models (including any "free" ones) can
change or disappear without notice, and there is no compliance/SLA guarantee.
Enterprise deployments should use Claude (Agent SDK), Codex, AWS Bedrock, or
Azure OpenAI instead.  See ``docs/docs/concepts/multi-provider.md``.

Model routing
-------------
OpenCode's native ``--model`` flag expects a ``provider/model`` string
(e.g. ``anthropic/claude-sonnet-4-5``).  AIFactory selects this runtime with an
``opencode:`` model prefix, e.g.::

    opencode:anthropic/claude-sonnet-4-5
    opencode:openai/gpt-4o

The ``opencode:`` prefix selects the *provider* (this class); it is stripped
before the remaining ``provider/model`` string is handed to the CLI's
``--model`` flag.

When no model is supplied, the default is resolved from the
``OPENCODE_DEFAULT_MODEL`` environment variable.  AIFactory deliberately does
**not** hardcode a free model: OpenCode's catalogue lives in the remote
``models.dev`` registry and free "zen" models (e.g. the former
``opencode/sonic``) are not guaranteed to exist.  When no usable model can be
resolved, the provider fails with a clear, actionable error asking you to pass
``opencode:<provider/model>`` (or set ``OPENCODE_DEFAULT_MODEL``).

Usage::

    from providers.opencode_agentic import OpenCodeAgenticProvider

    provider = OpenCodeAgenticProvider(
        model="opencode:anthropic/claude-sonnet-4-5",
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
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from providers import BaseLLMProvider
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)

#: OpenCode is a community / self-host tier provider — NOT enterprise-certified.
#: Its model catalogue is fetched from the remote ``models.dev`` registry, so
#: individual models can change/disappear and there is no compliance/SLA
#: guarantee.  Enterprise deployments should use Claude/Codex/Bedrock/Azure.
ENTERPRISE_CERTIFIED: bool = False
SUPPORT_TIER: str = "community"

_DEFAULT_OPENCODE_PATH: str = "opencode"
#: Environment variable that supplies the default OpenCode model when AIFactory
#: routes a build to OpenCode without an explicit ``opencode:<provider/model>``.
#: We do NOT hardcode a model: OpenCode's catalogue lives in the remote
#: ``models.dev`` registry and free "zen" models (e.g. the former
#: ``opencode/sonic``) are not guaranteed to exist.
_DEFAULT_MODEL_ENV_VAR: str = "OPENCODE_DEFAULT_MODEL"
_DEFAULT_TIMEOUT: int = 600  # 10 minutes for agentic tasks
# OpenCode model IDs are "provider/model" — allow alphanumerics plus . _ : / -
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")

#: Relative path of OpenCode's model catalogue inside its XDG cache dir.
#: At runtime OpenCode reads ``<XDG_CACHE_HOME or $HOME/.cache>/opencode/models.json``
#: (see ``src/provider/models.ts`` -> ``ModelsDev.get()`` / ``Global.Path.cache``
#: in the bundled CLI).  When that file is missing/empty it falls back to a small
#: catalogue compiled into the binary that omits newer models — see #291.
_CATALOGUE_REL_PATH: str = "opencode/models.json"
#: On startup OpenCode reads ``<cache>/opencode/version`` and, if it does not
#: equal the binary's baked-in ``CACHE_VERSION``, **recursively deletes the
#: entire cache directory** before recreating it (``src/global/index.ts``).
#: That wipe destroys any catalogue we pre-inject — so to make pre-warming
#: survive we must also write a matching version sentinel (copied from the
#: warm source, via ``.with_name("version")`` below) so the wipe is skipped.
#: See the analysis in #291.
#: Env var that disables OpenCode's background self-update check.  Set in the
#: build subprocess so a sandbox with blocked egress doesn't waste time/log noise
#: attempting an upgrade fetch it can never complete.
_DISABLE_AUTOUPDATE_ENV_VAR: str = "OPENCODE_DISABLE_AUTOUPDATE"


def _xdg_cache_home(env: Mapping[str, str]) -> Path | None:
    """Resolve OpenCode's XDG cache root from an environment mapping.

    Mirrors the ``xdg-basedir`` logic baked into the OpenCode CLI:
    ``XDG_CACHE_HOME`` if set, else ``$HOME/.cache``.  Returns ``None`` when
    neither is resolvable (no ``HOME``), in which case we cannot pre-warm.
    """
    xdg = env.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg)
    home = env.get("HOME", "").strip()
    if home:
        return Path(home) / ".cache"
    return None


def _find_warm_catalogue(env: Mapping[str, str]) -> Path | None:
    """Locate an existing, populated OpenCode model catalogue to pre-warm from.

    OpenCode caches the full ``models.dev`` registry at
    ``<cache>/opencode/models.json`` after any successful (interactive) run.  In
    AIFactory's build sandbox the network refresh of ``models.dev`` is blocked,
    so a build never repopulates this file; if the catalogue the subprocess
    reads is missing/empty, OpenCode silently falls back to a small catalogue
    compiled into the binary that omits newer models (e.g. ``claude-sonnet-4-5``),
    producing a fatal ``ProviderModelNotFoundError`` for every model (#291).

    We search known cache locations and return the first non-empty catalogue:
      1. The resolved XDG cache for the subprocess env.
      2. The real user ``$HOME/.cache`` (warmed by interactive use).

    Returns ``None`` if no usable catalogue exists anywhere.
    """
    candidates: list[Path] = []
    cache_root = _xdg_cache_home(env)
    if cache_root is not None:
        candidates.append(cache_root / _CATALOGUE_REL_PATH)
    # Fall back to the real user cache (interactive runs warm this even when the
    # subprocess env points XDG_CACHE_HOME elsewhere).
    try:
        home_cache = Path.home() / ".cache" / _CATALOGUE_REL_PATH
        if home_cache not in candidates:
            candidates.append(home_cache)
    except (RuntimeError, OSError):
        pass

    for path in candidates:
        try:
            # A real models.dev dump is megabytes; ``> 2`` rejects ``{}``/empty.
            if path.is_file() and path.stat().st_size > 2:
                return path
        except OSError:
            continue
    return None


def _strip_opencode_prefix(model: str) -> str:
    """Drop a leading ``opencode:`` from the model string.

    AIFactory routes the OpenCode runtime with ``opencode:<provider/model>`` so
    the prefix selects the provider; the CLI's ``--model`` flag itself wants the
    bare ``provider/model`` form (e.g. ``anthropic/claude-sonnet-4-5``,
    ``openai/gpt-4o``).
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
            (e.g. ``"opencode:anthropic/claude-sonnet-4-5"``).  The bare
            ``provider/model`` form is passed to the CLI's ``--model`` flag.
            When empty/omitted, the default is read from the
            ``OPENCODE_DEFAULT_MODEL`` environment variable; if neither is
            set, the provider raises a clear error at run time telling you to
            pass ``opencode:<provider/model>``.
        opencode_path: Path or command name for the ``opencode`` executable.
        timeout: Maximum seconds to wait for the subprocess.
        working_dir: Working directory for the subprocess (OpenCode operates on
            files relative to this directory).
        extra_args: Additional CLI flags appended to the ``run`` invocation.
    """

    def __init__(
        self,
        model: str = "",
        opencode_path: str = _DEFAULT_OPENCODE_PATH,
        timeout: int = _DEFAULT_TIMEOUT,
        working_dir: Path | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        if model and not _MODEL_NAME_RE.match(model):
            raise ValueError(
                f"Invalid model name '{model}': must be alphanumeric with . _ : / - separators"
            )
        # Resolution order: explicit (prefix-stripped) model -> OPENCODE_DEFAULT_MODEL
        # env override.  We intentionally do NOT fall back to a hardcoded free
        # model, since OpenCode's remote ``models.dev`` catalogue can drop it.
        # A missing model is surfaced as a clear error at run time (see
        # ``_build_command``), not papered over with a dead default.
        resolved_model = _strip_opencode_prefix(model).strip() if model else ""
        if not resolved_model:
            resolved_model = os.environ.get(_DEFAULT_MODEL_ENV_VAR, "").strip()
        self._model = resolved_model
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

            opencode run --model <provider/model> <prompt> [extra_args...]

        The prompt is passed as a **positional argument** to the ``run``
        subcommand.  The global ``-p`` / ``--prompt`` flag is NOT inherited by
        the ``run`` subcommand — passing ``-p <prompt>`` to ``opencode run``
        causes it to print the subcommand help and exit with code 1 without
        executing anything.  This was the root cause of issue #286 where every
        opencode subtask produced only the help text instead of real code.

        Note: the positional ``message`` form does not support multi-word
        prompts natively across shells without quoting, but when passed as a
        single Python list element to ``create_subprocess_exec`` no shell
        quoting is needed — Python passes it verbatim as argv[N].

        Raises:
            RuntimeError: If no usable model could be resolved (no explicit
                ``opencode:<provider/model>`` and no ``OPENCODE_DEFAULT_MODEL``).
                We refuse to guess a model, since OpenCode's free models are not
                guaranteed to exist in the remote ``models.dev`` registry.
        """
        if not self._model:
            raise RuntimeError(
                "No OpenCode model resolved. OpenCode does not have a guaranteed "
                "free default model (its catalogue comes from the remote "
                "models.dev registry, which can drop models). Pass an explicit "
                "model as 'opencode:<provider/model>' "
                "(e.g. 'opencode:anthropic/claude-sonnet-4-5') or set the "
                f"{_DEFAULT_MODEL_ENV_VAR} environment variable."
            )
        cmd: list[str] = [self._opencode_path, "run", "--model", self._model]
        # Pass prompt as a positional argument, not -p.  The `run` subcommand
        # accepts `message..` as positional args; the global `-p/--prompt` flag
        # is not valid inside the `run` subcommand and causes it to print help
        # and exit 1 without doing any work.  (#286)
        cmd.append(self._pending_prompt or "")
        if self._extra_args:
            cmd.extend(self._extra_args)
        return cmd

    def _build_subprocess_env(self) -> dict[str, str]:
        """Build the env for the OpenCode subprocess with a usable catalogue.

        OpenCode resolves its model catalogue at runtime from
        ``<XDG_CACHE_HOME or $HOME/.cache>/opencode/models.json`` and refreshes
        it from the remote ``models.dev`` registry.  AIFactory builds run in an
        OS sandbox where that egress is blocked, so the catalogue at the path
        OpenCode reads can be missing/empty — OpenCode then falls back to a
        small catalogue compiled into the binary that omits newer models (e.g.
        ``claude-sonnet-4-5``), and every such model fails with a fatal
        ``ProviderModelNotFoundError`` (#291).

        Naively copying a warm ``models.json`` into the cache is **not enough**:
        on startup OpenCode compares ``<cache>/opencode/version`` against its
        baked-in ``CACHE_VERSION`` and, on mismatch, ``rm -rf``'s the whole
        cache directory before reading the catalogue — wiping anything we
        injected.  So to make builds work offline we, when a warm source exists:

          * copy the warm ``models.json`` into the path OpenCode will read, and
          * copy the warm ``version`` sentinel alongside it so OpenCode's
            cache-version check passes and the destructive wipe is skipped, and
          * disable the background self-update fetch (pointless under blocked
            egress).

        This is best-effort: if no warm catalogue can be found we leave the env
        untouched and let OpenCode behave as before (the build may still fail
        for models absent from the embedded fallback — see the #291 docs note).
        """
        env = os.environ.copy()
        env.setdefault(_DISABLE_AUTOUPDATE_ENV_VAR, "1")

        target_root = _xdg_cache_home(env)
        if target_root is None:
            # No HOME/XDG to anchor a cache path — nothing we can pre-warm.
            return env
        target = target_root / _CATALOGUE_REL_PATH

        source = _find_warm_catalogue(env)
        if source is None or source == target:
            # Either no warm catalogue to copy from, or the target IS the warm
            # source (the real user cache) — in which case OpenCode reads it
            # directly and we must not clobber it.
            #
            # TODO(#291): when no warm catalogue exists anywhere (OpenCode has
            # never been run interactively on this host) we have nothing to
            # pre-warm from, so a sandboxed build for a model outside OpenCode's
            # embedded fallback will still fail. A fully self-contained fix would
            # bundle/fetch a pinned models.dev snapshot at install time. For now
            # this is documented as an OSS-tier limitation in
            # docs/docs/concepts/multi-provider.md.
            return env

        # Copy the warm catalogue AND its version sentinel.  The version file is
        # what stops OpenCode from wiping the cache dir on startup (see #291); if
        # the warm source has no sibling version file we skip it (OpenCode will
        # then wipe, but that is no worse than the un-warmed status quo).
        source_version = source.with_name("version")
        target_version = target.with_name("version")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if source_version.is_file():
                shutil.copyfile(source_version, target_version)
            logger.debug(
                "OpenCodeAgenticProvider: pre-warmed catalogue %s -> %s "
                "(version sentinel: %s) (#291)",
                source,
                target,
                "copied" if source_version.is_file() else "missing-at-source",
            )
        except OSError as exc:
            # Pre-warming is a convenience, never fatal: log and continue.
            logger.warning(
                "OpenCodeAgenticProvider: could not pre-warm models.dev "
                "catalogue %s -> %s: %s",
                source,
                target,
                exc,
            )
        return env

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
        # Pre-warm OpenCode's models.dev catalogue so the model resolves even
        # when the build sandbox blocks egress to models.dev (#291).
        env = self._build_subprocess_env()
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
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(self._timeout),
            )
        except TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise TimeoutError(f"OpenCode CLI timed out after {self._timeout}s.")

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        logger.debug(
            "OpenCodeAgenticProvider: finished returncode=%d stdout_len=%d stderr_len=%d",
            proc.returncode,
            len(stdout_text),
            len(stderr_text),
        )

        if proc.returncode != 0:
            # Guard against the "help text printed instead of running" scenario:
            # if the process failed AND the output looks like the CLI help page,
            # raise a descriptive error rather than yielding the help text as if
            # it were agent output.  This can happen when an unknown flag is
            # passed (the previous bug where -p was used instead of a positional
            # argument) or when the subcommand arguments are malformed.
            # We detect the help page by its stable opening line.  (#286)
            _looks_like_help = "run opencode with a message" in stdout_text or (
                "opencode run [" in stdout_text
            )
            if _looks_like_help:
                raise RuntimeError(
                    "OpenCode CLI printed its help page instead of running — "
                    "the command was likely malformed.  "
                    f"Command: {' '.join(cmd)}"
                )
            if not stdout_text:
                error_detail = stderr_text or f"exit code {proc.returncode}"
                raise RuntimeError(f"OpenCode CLI error: {error_detail}")

        if stderr_text:
            logger.warning(
                "OpenCode CLI stderr (first 500 chars): %s", stderr_text[:500]
            )

        response_text = stdout_text if stdout_text else "(no output from OpenCode CLI)"
        yield AssistantMessage(content=[TextBlock(text=response_text)])

    async def __aenter__(self) -> OpenCodeAgenticProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._pending_prompt = None


__all__ = [
    "ENTERPRISE_CERTIFIED",
    "SUPPORT_TIER",
    "OpenCodeAgenticProvider",
]
