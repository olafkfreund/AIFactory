"""
Optional OS sandbox for agent subprocesses (#363, slice 2).

The command allowlist (#321), env-secret scrub (#363 slice 1), and fs/process
validators (#364) harden agent execution, but there is still no OS boundary —
a determined command can write outside the worktree or reach the network. This
module adds an opt-in bubblewrap (``bwrap``) sandbox around the agent
subprocess: read-write only the active worktree, read-only system dirs, private
PID/IPC/UTS namespaces and ``/tmp``.

It is **off by default** and a pure passthrough when disabled or when ``bwrap``
is not installed, so wiring it into the spawn path is zero-risk until an
operator opts in via ``AIFACTORY_AGENT_SANDBOX``:

- unset / ``off`` → no sandbox (default).
- ``fs``          → filesystem + namespace isolation; network left intact (the
  agent must reach Claude and the control-plane API).
- ``strict``      → ``fs`` plus ``--unshare-net`` (no egress). Opt-in; breaks
  outbound connectivity unless a proxy is provided — for air-gapped/proxied
  deployments only.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence

# System directories the sandbox exposes read-only (``-try`` so a layout that
# lacks one — e.g. no separate /lib64 — doesn't fail the spawn).
_SYSTEM_RO_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")

_VALID_MODES = ("fs", "strict")


def _mode() -> str:
    return (os.environ.get("AIFACTORY_AGENT_SANDBOX") or "off").strip().lower()


def _bwrap_path() -> str | None:
    return shutil.which("bwrap")


def is_enabled() -> bool:
    """True when a sandbox mode is requested AND ``bwrap`` is available."""
    return _mode() in _VALID_MODES and _bwrap_path() is not None


def build_sandboxed_command(
    cmd: Sequence[str],
    worktree_root: str | os.PathLike,
    *,
    mode: str | None = None,
) -> list[str]:
    """Return ``cmd`` wrapped to run inside a bubblewrap sandbox, or ``cmd``
    unchanged when the sandbox is disabled / unavailable.

    The worktree is the only writable path; everything else is read-only or a
    private tmpfs. Network is isolated only in ``strict`` mode.
    """
    selected = (mode or _mode()).strip().lower()
    bwrap = _bwrap_path()
    if selected not in _VALID_MODES or not bwrap:
        return list(cmd)

    root = os.path.abspath(str(worktree_root))
    args: list[str] = [
        bwrap,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    for d in _SYSTEM_RO_DIRS:
        args += ["--ro-bind-try", d, d]
    # Read-write ONLY the active worktree, and start there.
    args += ["--bind", root, root, "--chdir", root]
    if selected == "strict":
        args += ["--unshare-net"]
    args += ["--", *cmd]
    return args
