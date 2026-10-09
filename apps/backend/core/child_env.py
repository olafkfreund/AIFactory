"""Scrubbed environment for child processes (#1680).

Shared by the web server (``server/utils/subprocess_env.py``) and the backend
code the server runs in-process (git push/fetch, ``gh``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from core.auth import is_denied_env_key

# Stripped so a child never silently bills the direct Anthropic API.
_STRIP_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE")

GITHUB_KEEP: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")


def child_env(
    keep: Iterable[str] = (), extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return a scrubbed copy of ``os.environ`` for a child process.

    Drops every host secret ``is_denied_env_key`` matches plus ``_STRIP_VARS``,
    restores the ``keep`` names that are set, applies ``extra``, and disables
    git hooks via git env config (an existing ``GIT_CONFIG_*`` entry survives).
    Build it per call: ``os.environ`` changes at runtime.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not is_denied_env_key(k) and k not in _STRIP_VARS
    }
    env.update({k: os.environ[k] for k in keep if k in os.environ})
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
