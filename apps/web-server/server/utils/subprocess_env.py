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
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.auth import is_denied_env_key  # noqa: E402 — needs sys.path above

# Env vars we explicitly strip from subprocess environments to prevent
# silent direct-API billing. Keep this list narrow — anything not in here
# is passed through unchanged.
_STRIP_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_FILE",
)


GITHUB_KEEP: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")
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
    """Return a scrubbed copy of ``os.environ`` for a child process (#1680).

    Drops every host secret ``is_denied_env_key`` matches plus ``_STRIP_VARS``,
    restores the ``keep`` names that are set, applies ``extra``, and disables
    git hooks via git env config (an existing ``GIT_CONFIG_*`` entry survives).
    Build it per call: ``os.environ`` changes at runtime.
    """
    env = {k: v for k, v in os.environ.items() if not is_denied_env_key(k) and k not in _STRIP_VARS}
    env.update({k: os.environ[k] for k in keep if k in os.environ})
    _inject_traceparent(env)
    if extra:
        env.update(extra)
    try:
        n = max(int(env.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        n = 0
    env[f"GIT_CONFIG_KEY_{n}"] = "core.hooksPath"
    env[f"GIT_CONFIG_VALUE_{n}"] = "/dev/null"
    env["GIT_CONFIG_COUNT"] = str(n + 1)
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


def _inject_traceparent(env: dict[str, str]) -> None:
    """Add ``TRACEPARENT`` to ``env`` when an OTel span is active.

    Wrapped in try/except so this helper can never crash a
    subprocess spawn — tracing is always optional.
    """
    with contextlib.suppress(Exception):
        from ..observability.tracing import get_current_traceparent

        tp = get_current_traceparent()
        if tp:
            env["TRACEPARENT"] = tp
