"""Scrubbed environment for child processes (#1680).

Shared by the web server (``server/utils/subprocess_env.py``) and the backend
code the server runs in-process (git push/fetch, ``gh``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping

from core.auth import is_denied_env_key

logger = logging.getLogger(__name__)

# Stripped so a child never silently bills the direct Anthropic API.
_STRIP_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_FILE",
    "AIFACTORY_TRUSTED_CONTRACT",
)

GITHUB_KEEP: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")

# Credential-shaped names the agent scrub lets through (OAuth token, Context7,
# LangChain, S3 access key, ...). git, gh and tools never need them; a child
# that does names them in ``keep``. Not applied to the agent runner: operator
# MCP credentials can use any env name (#1680).
_CREDENTIAL_NAME = re.compile(r"(_KEY|_TOKEN)$", re.IGNORECASE)


def child_env(
    keep: Iterable[str] = (),
    extra: Mapping[str, str] | None = None,
    *,
    runner: bool = False,
) -> dict[str, str]:
    """Return a scrubbed copy of ``os.environ`` for a child process.

    Drops every host secret ``is_denied_env_key`` matches plus ``_STRIP_VARS``,
    and, unless ``runner``, every ``*_KEY``/``*_TOKEN`` name;
    restores the ``keep`` names that are set, applies ``extra``, and disables
    git hooks, fsmonitor and the ext:: transport via git env config. The pins
    are appended last (an existing ``GIT_CONFIG_*`` entry keeps its lower index).
    Build it per call: ``os.environ`` changes at runtime.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not is_denied_env_key(k)
        and k not in _STRIP_VARS
        and (runner or not _CREDENTIAL_NAME.search(k))
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
    env[f"GIT_CONFIG_KEY_{n + 1}"] = "core.fsmonitor"
    env[f"GIT_CONFIG_VALUE_{n + 1}"] = "false"
    env[f"GIT_CONFIG_KEY_{n + 2}"] = "protocol.ext.allow"
    env[f"GIT_CONFIG_VALUE_{n + 2}"] = "never"
    env["GIT_CONFIG_COUNT"] = str(n + 3)
    return env


_GITHUB_ORIGIN = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
)


def lfs_push_argv(push_url_output: str, ref: str) -> list[str] | None:
    """Pinned ``git lfs push`` argv for a plain github.com origin, else None (#1690).

    Hooks are off (#1680), so git-lfs's pre-push no longer uploads objects. The
    endpoint is derived from origin and passed as both ``lfs.url`` and
    ``lfs.pushurl`` so repo config and ``.lfsconfig`` cannot redirect it.
    """
    if shutil.which("git-lfs") is None:
        return None
    # Fixed messages: the raw output can carry userinfo.
    if "\n" in push_url_output or "\r" in push_url_output:
        logger.warning("origin push URL is not a single line; skipping LFS upload")
        return None
    m = _GITHUB_ORIGIN.fullmatch(push_url_output)
    if m is None or {".", ".."} & set(m.groups()):
        logger.warning(
            "origin is not a plain https://github.com/<owner>/<repo> URL; "
            "skipping LFS upload"
        )
        return None
    e = f"https://github.com/{m[1]}/{m[2]}.git/info/lfs"
    return [
        "git",
        "-c",
        f"lfs.url={e}",
        "-c",
        f"lfs.pushurl={e}",
        "-c",
        f"lfs.{e}.locksverify=false",
        "-c",
        "lfs.allowincompletepush=false",
        "lfs",
        "push",
        "origin",
        ref,
    ]
