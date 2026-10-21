"""Process hardening shared by the web server and the agent runners (#1680)."""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import sys
import time
from pathlib import Path

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


def require_non_dumpable() -> None:
    """Fail closed: a process that cannot hide its secrets must not run."""
    if not make_non_dumpable():
        raise SystemExit("prctl(PR_SET_DUMPABLE, 0) failed")


def _process_status(pid: int) -> tuple[int, str, int] | None:
    try:
        fields = dict(
            line.split(":", 1)
            for line in (Path("/proc") / str(pid) / "status").read_text().splitlines()
            if ":" in line
        )
        return (
            int(fields["Uid"].split()[0]),
            fields["State"].split()[0],
            int(fields["PPid"].split()[0]),
        )
    except (OSError, KeyError, IndexError, ValueError):
        return None


def _stray_pids() -> list[int]:
    """Find live same-uid processes outside this process's ancestor chain."""
    ancestors: set[int] = set()
    pid = os.getpid()
    while pid and pid not in ancestors:
        ancestors.add(pid)
        status = _process_status(pid)
        if status is None:
            break
        pid = status[2]

    strays = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        status = _process_status(pid)
        if (
            status is not None
            and status[0] == os.getuid()
            and status[1] != "Z"
            and pid not in ancestors
        ):
            strays.append(pid)
    return strays


def kill_stray_processes(rounds: int = 5) -> bool:
    """Stop agent-started processes before a build Job mints push credentials."""
    for _ in range(rounds):
        pids = _stray_pids()
        if not pids:
            return True
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    return not _stray_pids()


def in_build_job() -> bool:
    return bool(os.environ.get("JOB_ID") and os.environ.get("FACTORY_SERVICE"))
