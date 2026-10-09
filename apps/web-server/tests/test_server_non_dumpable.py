"""The web server must make itself non-dumpable so same-uid agents cannot read
its secrets from /proc/<pid>/environ."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux /proc only"
)

WEB_SERVER = Path(__file__).resolve().parents[1]

CHILD = """
import ctypes, sys
from server.main import _make_non_dumpable
_make_non_dumpable()
print("DUMPABLE", ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), flush=True)  # PR_GET_DUMPABLE
sys.stdin.read()
"""


def test_server_environ_is_unreadable_after_startup_hardening() -> None:
    env = {**os.environ, "NON_DUMPABLE_CANARY": "s3cret"}
    child = subprocess.Popen(  # noqa: S603 — fixed argv, our own interpreter
        [sys.executable, "-c", CHILD],
        cwd=WEB_SERVER,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None and child.stdout is not None
    try:
        # Importing server.main logs to stdout first; skip to our line.
        line = next(ln for ln in child.stdout if ln.startswith("DUMPABLE"))
        assert line.split() == ["DUMPABLE", "0"]
        if os.geteuid() != 0:  # root has CAP_SYS_PTRACE and may still read it
            with pytest.raises(PermissionError):
                Path(f"/proc/{child.pid}/environ").read_bytes()
    finally:
        child.stdin.close()
        child.wait(timeout=30)
