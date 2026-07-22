"""
Optional OS sandbox for agent subprocesses (#363, slice 2).

The command allowlist (#321), env-secret scrub (#363 slice 1), and fs/process
validators (#364) harden agent execution, but there is still no OS boundary —
a determined command can write outside the worktree or reach the network. This
module adds an opt-in bubblewrap (``bwrap``) sandbox around the agent
subprocess: read-write only the active worktree, read-only system dirs, private
PID/IPC/UTS namespaces and ``/tmp``.

It is a pure passthrough when disabled or when ``bwrap`` is not installed, so
wiring it into the spawn path is safe everywhere. Mode is set via
``AIFACTORY_AGENT_SANDBOX``:

- unset / ``off`` → no sandbox.
- ``fs``          → filesystem + namespace isolation; network left intact (the
  agent must reach Claude and the control-plane API).
- ``strict``      → ``fs`` plus ``--unshare-net`` (no egress). Opt-in; breaks
  outbound connectivity unless a proxy is provided — for air-gapped/proxied
  deployments only.

PID-namespace isolation (``--unshare-pid`` + a fresh ``/proc``) needs privileges
an unprivileged Kubernetes pod (e.g. k3d) does not have — there it fails the
spawn outright, which is why the sandbox was previously inert on-cluster
(#363 AC1). It is a hardening *bonus*, not required for the filesystem boundary,
so the default keeps the host PID namespace with a read-only ``/proc`` and runs
unprivileged in-pod. Set ``AIFACTORY_AGENT_SANDBOX_PIDNS=1`` to opt into PID
isolation on privileged hosts.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from functools import lru_cache

_log = logging.getLogger(__name__)

# System directories the sandbox exposes read-only (``-try`` so a layout that
# lacks one — e.g. no separate /lib64 — doesn't fail the spawn).
_SYSTEM_RO_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")

_VALID_MODES = ("fs", "strict")


def _mode() -> str:
    return (os.environ.get("AIFACTORY_AGENT_SANDBOX") or "off").strip().lower()


def _bwrap_path() -> str | None:
    return shutil.which("bwrap")


@lru_cache(maxsize=1)
def _bwrap_works(bwrap: str) -> bool:
    """True when ``bwrap`` can actually spawn here — not just that it's installed.

    Unprivileged bwrap needs a **user namespace** to set up its other namespaces.
    A node whose kernel disallows unprivileged userns
    (``kernel.unprivileged_userns_clone=0`` / ``user.max_user_namespaces=0``) makes
    bwrap fail at exec with *"No permissions to create a new namespace"* — which
    would break EVERY wrapped command (git commit, etc.), not just isolate it.
    Probe once with a trivial invocation and cache it (the kernel capability can't
    change under a running process), so we degrade to an unwrapped passthrough
    instead of failing every command. Same end state as bwrap being absent.
    """
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [bwrap, "--ro-bind", "/", "/", "--tmpfs", "/tmp", "--", "true"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        _log.warning(
            "bwrap is installed but cannot create a namespace here "
            "(rc=%s: %s) — the agent sandbox is DISABLED and commands run "
            "unwrapped. Enable unprivileged user namespaces on the build node "
            "(sysctl kernel.unprivileged_userns_clone=1) to restore isolation.",
            r.returncode,
            (r.stderr or b"").decode(errors="replace").strip()[:200],
        )
        return False
    return True


def _pidns_enabled() -> bool:
    """Opt into PID-namespace isolation (needs privilege; off on unprivileged k3d).

    The fresh ``/proc`` that ``--unshare-pid`` requires can't be mounted in an
    unprivileged pod, so this is off by default and only enabled where the host
    grants the capability via ``AIFACTORY_AGENT_SANDBOX_PIDNS``.
    """
    return (os.environ.get("AIFACTORY_AGENT_SANDBOX_PIDNS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_enabled() -> bool:
    """True when a sandbox mode is requested AND ``bwrap`` is available AND works."""
    if _mode() not in _VALID_MODES:
        return False
    bwrap = _bwrap_path()
    return bwrap is not None and _bwrap_works(bwrap)


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
    if selected not in _VALID_MODES or not bwrap or not _bwrap_works(bwrap):
        return list(cmd)

    root = os.path.abspath(str(worktree_root))
    args: list[str] = [
        bwrap,
        "--die-with-parent",
        "--unshare-ipc",
        "--unshare-uts",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    # PID isolation needs a fresh /proc, which requires privilege the
    # unprivileged in-pod path lacks. Default: keep the host PID namespace with a
    # read-only /proc (tools still read /proc/self, cpuinfo, etc.); opt into full
    # PID isolation only where the host allows it.
    if _pidns_enabled():
        args += ["--unshare-pid", "--proc", "/proc"]
    else:
        args += ["--ro-bind-try", "/proc", "/proc"]
    for d in _SYSTEM_RO_DIRS:
        args += ["--ro-bind-try", d, d]
    # Read-write ONLY the active worktree, and start there.
    args += ["--bind", root, root, "--chdir", root]
    if selected == "strict":
        args += ["--unshare-net"]
    args += ["--", *cmd]
    return args
