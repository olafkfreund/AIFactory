"""Process hardening shared by the web server and the agent runners (#1680)."""

from __future__ import annotations

import ctypes
import sys

_PR_SET_DUMPABLE = 4


def make_non_dumpable() -> bool:
    """Hide this process's environ and memory from same-uid processes.

    A non-dumpable process's /proc files are root-owned, so a same-uid agent
    cannot read its secrets. Linux only; elsewhere a no-op returning True.
    Returns False when the prctl call fails.
    """
    if not sys.platform.startswith("linux"):
        return True
    libc = ctypes.CDLL(None, use_errno=True)
    return bool(libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0)
