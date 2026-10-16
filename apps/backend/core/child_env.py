"""Scrubbed environment for child processes (#1680).

Shared by the web server (``server/utils/subprocess_env.py``) and the backend
code the server runs in-process (git push/fetch, ``gh``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping

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
    also set the ssh command (``ssh``), pager (``cat``) and editor (``true``);
    ``GIT_SSH_COMMAND``, ``GIT_PAGER`` or ``GIT_EDITOR`` in the server env
    overrides them. The pins are appended last (an existing ``GIT_CONFIG_*``
    entry keeps its lower index).
    Build it per call: ``os.environ`` changes at runtime.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not is_denied_env_key(k)
        # Linux env names are case-sensitive: a lowercase copy is its own variable.
        and k.upper() not in _STRIP_VARS
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
    env[f"GIT_CONFIG_KEY_{n + 3}"] = "core.sshCommand"
    env[f"GIT_CONFIG_VALUE_{n + 3}"] = "ssh"
    env[f"GIT_CONFIG_KEY_{n + 4}"] = "core.pager"
    env[f"GIT_CONFIG_VALUE_{n + 4}"] = "cat"
    env[f"GIT_CONFIG_KEY_{n + 5}"] = "core.editor"
    env[f"GIT_CONFIG_VALUE_{n + 5}"] = "true"
    env["GIT_CONFIG_COUNT"] = str(n + 6)
    return env


_GITHUB_ORIGIN = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
)


def lfs_push_argv(push_url_output: str, ref: str) -> list[str] | None:
    """Pinned ``git lfs push`` argv for a plain github.com origin, else None (#1690).

    Hooks are off (#1680), so git-lfs's pre-push no longer uploads objects. The
    endpoint is derived from origin and passed as both ``lfs.url`` and
    ``lfs.pushurl``, which overrides repo ``lfs.*`` config and ``.lfsconfig``.
    That is not enough alone: git-lfs still applies ``url.*.insteadOf`` and runs
    ``lfs.customtransfer.*`` programs, so callers MUST refuse the upload when the
    repo defines either (``git config --get-regexp '^(lfs\\.customtransfer\\.|url\\.)'``
    exits 0 or errors).
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


LFS_UPLOAD_TIMEOUT = 600


def lfs_upload(
    run: Callable[[list[str], int], tuple[int, str, str]],
    ref: str,
) -> str | None:
    """Upload ``ref``'s LFS objects before a push; None = done or not needed (#1721).

    Blocking for up to ``LFS_UPLOAD_TIMEOUT``: async callers use a thread.

    ``run(argv, timeout)`` runs git in the repo with the push's env and returns
    ``(rc, stdout, stderr)``. Without ``git-lfs`` on PATH, for non-GitHub or ssh
    origins, or when ``.gitattributes`` at ``ref`` has no ``filter=lfs``, it
    returns None. The upload gets ``LFS_UPLOAD_TIMEOUT``. Best-effort, because no
    image before #1721 shipped git-lfs, so no push ever failed on it: an upload
    failure, a timeout or the ``url.*``/customtransfer refusal logs a warning and
    returns None. Only the safety-net refusal (a local ``filter.lfs.clean`` with
    no supported endpoint) is returned, and the caller skips the push.
    """
    if shutil.which("git-lfs") is None:
        return None
    url_rc, out, _ = run(["git", "remote", "get-url", "--push", "--all", "origin"], 60)
    url = out.strip()
    if url_rc != 0 or not url.startswith("https://github.com/"):
        return None
    gate, _, _ = run(
        [
            "git",
            "grep",
            "-q",
            "-F",
            "-e",
            "filter=lfs",
            ref,
            "--",
            ":(glob)**/.gitattributes",
        ],
        60,
    )
    if gate != 0:
        return None
    argv = lfs_push_argv(url, ref)
    if argv is None:
        # Safety net: only a repo with the clean filter on can hold new pointers;
        # without it commits are raw blobs and the push goes ahead as before.
        filt, _, _ = run(["git", "config", "--local", "--get", "filter.lfs.clean"], 60)
        return (
            None
            if filt != 0
            else (
                "LFS repo but no supported LFS endpoint for this origin; "
                "refusing to push pointers without objects"
            )
        )
    check_rc, _, _ = run(
        [
            "git",
            "config",
            "--name-only",
            "--get-regexp",
            r"^(lfs\.customtransfer\.|url\.)",
        ],
        60,
    )
    if check_rc != 1:
        err: str | None = "LFS step refused (lfs.customtransfer.* or url.* defined)"
    else:
        try:
            upload_rc, _, stderr = run(argv, LFS_UPLOAD_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            err = f"git lfs push timed out after {exc.timeout} s"
        else:
            err = None if upload_rc == 0 else (stderr.strip() or "git lfs push failed")
    if err:
        logger.warning("LFS upload failed; pushing without it: %s", err[:200])
    return None
