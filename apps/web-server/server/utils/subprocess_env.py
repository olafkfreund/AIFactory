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
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.child_env import (  # noqa: E402 — needs sys.path above
    GITHUB_KEEP,
    child_env as _core_child_env,
)

__all__ = ["GITHUB_KEEP", "RUNNER_KEEP", "child_env", "make_subprocess_env"]

RUNNER_KEEP: tuple[str, ...] = (
    *GITHUB_KEEP,
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "VOYAGE_API_KEY",
)


def child_env(keep: Iterable[str] = (), extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Core ``child_env`` plus ``TRACEPARENT`` when a span is active (#1680)."""
    env: dict[str, str] = _core_child_env(keep=keep, extra={**_traceparent(), **(extra or {})})
    return env


def make_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    strip_anthropic_api_key: bool = True,
) -> dict[str, str]:
    """Return a scrubbed env for LLM runner subprocesses.

    Keeps ``RUNNER_KEEP`` (GitHub and provider keys) and the SDK auth path
    (``CLAUDE_CODE_OAUTH_TOKEN``, via the non-denied default). The Anthropic
    direct-API key is stripped unless ``strip_anthropic_api_key=False``, which
    only an explicitly consented batch invocation may pass.
    """
    keep = RUNNER_KEEP if strip_anthropic_api_key else (*RUNNER_KEEP, "ANTHROPIC_API_KEY")
    return child_env(keep=keep, extra=extra)


def _traceparent() -> dict[str, str]:
    """``TRACEPARENT`` when an OTel span is active; tracing must never crash a spawn."""
    with contextlib.suppress(Exception):
        from ..observability.tracing import get_current_traceparent

        tp = get_current_traceparent()
        if tp:
            return {"TRACEPARENT": tp}
    return {}
