"""Feed a GitHub token to ``git`` without putting it in argv (AIFactory#1366).

``/proc/<pid>/cmdline`` is world-readable on Linux. A push URL of the shape
``https://x-access-token:<token>@github.com/...`` handed to ``git push`` becomes
an argv element of the child, so every uid on the host can read the token for as
long as that child lives. Redacting the log does not help — the kernel is the one
publishing it. ``/proc/<pid>/environ`` is owner-only, and that asymmetry is the
whole point of the move.

So the URL carries the **username only** (which is what makes git ask for a
password) and the password arrives out-of-band through ``GIT_ASKPASS``. This is
the shape proven in PFactory#616 / AIFactory#1362 (``project_workspace_service``);
``apps/web-server`` is a separate ``sys.path`` root, so its private copy cannot be
imported from ``apps/backend`` — this module is the ``apps/backend`` one, shared
by every backend caller rather than re-derived per call site.

:func:`authed_push_url` yields the URL and the env **together**, so the askpass
vars can only ever accompany a URL this module rewrote. That matters: making the
credential unconditional would offer the token to whatever host a caller-supplied
remote happens to name. A non-github.com remote (or a missing token) yields the
URL untouched and a plain environment.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

#: Only https github.com remotes are credentialed; ssh remotes carry their own
#: auth, and any other host must not be offered this token.
GITHUB_HTTPS_PREFIX = "https://github.com/"

#: Conventional placeholder username for a GitHub PAT / app installation token.
USERNAME = "x-access-token"

# Tiny POSIX askpass helper. git invokes it as ``<script> "<prompt>"`` and reads
# the answer from stdout. Both values come from the environment — never argv.
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  Username*) printf '%s' "$GIT_USER" ;;
  *)         printf '%s' "$GIT_PASS" ;;
esac
"""


def github_token() -> str:
    """The ambient GitHub token, or ``""`` when the process has none."""
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


@contextlib.contextmanager
def authed_push_url(url: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield ``(url_for_git, env_for_git)`` for an authenticated git operation.

    When a token is present and ``url`` is an https github.com remote, the yielded
    URL carries the username only and the yielded env adds ``GIT_ASKPASS`` (a
    ``0700`` helper script, removed on exit) plus ``GIT_USER`` / ``GIT_PASS``.
    ``GIT_TERMINAL_PROMPT=0`` keeps git from blocking on a prompt if the helper
    is ever unusable.

    Otherwise the URL is yielded unchanged with a plain copy of ``os.environ`` —
    no credential is offered to a host this module did not build the URL for.

    The env is always a complete environment, ready to hand to
    ``subprocess.run(env=...)``.
    """
    token = github_token()
    if not token or not url.startswith(GITHUB_HTTPS_PREFIX):
        yield url, dict(os.environ)
        return

    authed = f"https://{USERNAME}@{url[len('https://') :]}"
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
        mode="w", prefix="git-askpass-", suffix=".sh", delete=False
    )
    try:
        handle.write(_ASKPASS_SCRIPT)
        handle.close()
        Path(handle.name).chmod(stat.S_IRWXU)  # 0700 — owner-only rwx
        yield (
            authed,
            {
                **os.environ,
                "GIT_ASKPASS": handle.name,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_USER": USERNAME,
                "GIT_PASS": token,
            },
        )
    finally:
        with contextlib.suppress(OSError):
            Path(handle.name).unlink()
