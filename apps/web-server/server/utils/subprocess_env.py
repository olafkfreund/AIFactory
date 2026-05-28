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

import os
from collections.abc import Mapping

# Env vars we explicitly strip from subprocess environments to prevent
# silent direct-API billing. Keep this list narrow — anything not in here
# is passed through unchanged.
_STRIP_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_FILE",
)


def make_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    strip_anthropic_api_key: bool = True,
) -> dict[str, str]:
    """Return a copy of ``os.environ`` safe to pass to ``subprocess.*``.

    By default removes the Anthropic direct-API credentials so subprocesses
    AIFactory spawns can never silently bill the user's API account. The
    SDK auth path (``CLAUDE_CODE_OAUTH_TOKEN``) is preserved.

    Args:
        extra: Optional mapping of additional vars to set on top of the
            scrubbed env.
        strip_anthropic_api_key: Caller can pass ``False`` only when the
            spawned process explicitly NEEDS the direct API key — e.g.
            an opt-in batch invocation that the user has consented to
            via Settings. Default ``True`` matches AIFactory's policy.

    Returns:
        A plain dict suitable for ``env=`` on ``subprocess.*`` calls.
    """
    env = os.environ.copy()
    if strip_anthropic_api_key:
        for var in _STRIP_VARS:
            env.pop(var, None)

    # Inject W3C TRACEPARENT when called inside an OTel span (Epic
    # #35 #42 PR-1). The agent subprocess's own OTel SDK reads
    # ``TRACEPARENT`` on init to seed its root context as a child
    # of the web-server's span — so a single trace covers the HTTP
    # request through to the LLM call. Best-effort: if OTel isn't
    # installed or there's no active span, the env var is just
    # omitted (subprocess starts a fresh root trace).
    _inject_traceparent(env)

    if extra:
        env.update(extra)
    return env


def _inject_traceparent(env: dict[str, str]) -> None:
    """Add ``TRACEPARENT`` to ``env`` when an OTel span is active.

    Wrapped in try/except so this helper can never crash a
    subprocess spawn — tracing is always optional.
    """
    try:
        from ..observability.tracing import get_current_traceparent
        tp = get_current_traceparent()
        if tp:
            env["TRACEPARENT"] = tp
    except Exception:
        pass
