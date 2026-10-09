# ruff: noqa: N818, PLC0415, S603
"""Agent runners make themselves non-dumpable before spawning (#1680)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))


class _Hardened(Exception):
    pass


def _boom() -> bool:
    raise _Hardened


def test_create_client_hardens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from core import client, process_hardening

    monkeypatch.setattr(process_hardening, "make_non_dumpable", _boom)
    with pytest.raises(_Hardened):
        client.create_client(tmp_path, tmp_path, "claude-sonnet-4-5")


def test_create_simple_client_hardens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from core import process_hardening, simple_client

    monkeypatch.setattr(process_hardening, "make_non_dumpable", _boom)
    with pytest.raises(_Hardened):
        simple_client.create_simple_client(
            agent_type="commit_message", model="claude-haiku-4-5", cwd=tmp_path
        )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
def test_make_non_dumpable_clears_flag() -> None:
    code = (
        "import ctypes, sys; sys.path.insert(0, sys.argv[1]);"
        "from core.process_hardening import make_non_dumpable;"
        "assert make_non_dumpable();"
        "print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))"  # PR_GET_DUMPABLE
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(_BACKEND)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "0"
