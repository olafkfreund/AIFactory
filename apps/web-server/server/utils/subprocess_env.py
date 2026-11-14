"""
Subprocess environment helper.

AIFactory is OAuth-only by design — the Claude Agent SDK should never see
``ANTHROPIC_API_KEY`` because that would silently bill against the user's
direct API account instead of their Claude Code subscription
(see ``apps/backend/core/auth.py`` for the canonical policy comment).

Every place AIFactory spawns a subprocess that may run a Claude CLI / SDK
call must build its env via ``make_subprocess_env()`` instead of bare
``os.environ.copy()``. The user's interactive PTY shell is the deliberate
exception — that's their own shell, they expect their normal env.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core import child_env as _core  # noqa: E402 — needs sys.path above
from core.git_credentials import (  # noqa: E402
    github_env,
    github_token,
    sweep_github_dirs,
)

GITHUB_KEEP = _core.GITHUB_KEEP
lfs_push_argv = _core.lfs_push_argv
merge_driver_pins = _core.merge_driver_pins
pin_git_config = _core.pin_git_config
lfs_upload = _core.lfs_upload

__all__ = [
    "AGENT_GITHUB_TOKEN_VAR",
    "GITHUB_KEEP",
    "RUNNER_KEEP",
    "agent_github_token",
    "child_env",
    "dotenv_key_blocked",
    "github_env",
    "github_names",
    "github_token",
    "lfs_push_argv",
    "lfs_upload",
    "make_subprocess_env",
    "merge_driver_pins",
    "pin_git_config",
    "runner_github_env",
    "sweep_github_dirs",
]

RUNNER_KEEP: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "VOYAGE_API_KEY",
    "OLLAMA_API_KEY",
)

AGENT_GITHUB_TOKEN_VAR = "AIFACTORY_AGENT_GITHUB_TOKEN"  # noqa: S105
_blank_agent_token_warned = False
_log = logging.getLogger(__name__)


def github_names() -> tuple[str, ...]:
    from core.mcp_credentials import _load_operator_config  # noqa: PLC0415

    names = (*GITHUB_KEEP, "GITHUB_PERSONAL_ACCESS_TOKEN")
    alias = (_load_operator_config().get("github") or {}).get("tokenEnv")
    return (*names, alias) if alias and alias not in names else names


def agent_github_token() -> str:
    global _blank_agent_token_warned  # noqa: PLW0603

    token = os.environ.get(AGENT_GITHUB_TOKEN_VAR, "").strip()
    if (
        AGENT_GITHUB_TOKEN_VAR in os.environ
        and not token
        and not _blank_agent_token_warned
    ):
        _log.warning(
            "Agent GitHub token is blank; runner children fall back to the "
            "control plane's GitHub token"
        )
        _blank_agent_token_warned = True
    return token


def runner_github_env() -> dict[str, str]:
    token = agent_github_token()
    if token:
        # The alias too: the runners load_dotenv() themselves, and a value
        # there for an absent alias would win in the MCP probe.
        return dict.fromkeys(github_names(), token)
    return {name: os.environ[name] for name in github_names() if os.environ.get(name)}


def dotenv_key_blocked(key: str) -> bool:
    return key == AGENT_GITHUB_TOKEN_VAR or bool(
        agent_github_token() and key in github_names()
    )


def child_env(
    keep: Iterable[str] = (),
    extra: Mapping[str, str] | None = None,
    *,
    runner: bool = False,
) -> dict[str, str]:
    """Core ``child_env`` plus ``TRACEPARENT`` when a span is active (#1680)."""
    env: dict[str, str] = _core.child_env(
        keep=keep, extra={**_traceparent(), **(extra or {})}, runner=runner
    )
    return env


def make_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    strip_anthropic_api_key: bool = True,
    github: bool = False,
) -> dict[str, str]:
    """Return a scrubbed env for LLM runner subprocesses.

    Keeps ``RUNNER_KEEP`` provider keys and the SDK auth path
    (``CLAUDE_CODE_OAUTH_TOKEN``, via the non-denied default). The Anthropic
    direct-API key is stripped unless ``strip_anthropic_api_key=False``, which
    only an explicitly consented batch invocation may pass.
    """
    keep = (
        RUNNER_KEEP
        if strip_anthropic_api_key
        else (*RUNNER_KEEP, "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE")
    )
    env = child_env(keep=keep, extra=extra, runner=True)
    for name in github_names():
        env.pop(name, None)
    if github:
        env.update(runner_github_env())
    return env


def _traceparent() -> dict[str, str]:
    """``TRACEPARENT`` when an OTel span is active; tracing must never crash a spawn."""
    with contextlib.suppress(Exception):
        from ..observability.tracing import get_current_traceparent

        tp = get_current_traceparent()
        if tp:
            return {"TRACEPARENT": tp}
    return {}
