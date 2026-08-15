"""An unreadable credentials file must not read as an absent one (Factory#718).

``_get_token_from_windows_credential_files`` walked four candidate paths as:

    if os.path.exists(cred_path):
        with open(cred_path) as f:
            ...

Two problems, and the second is the one that bites. The check-then-open is a
TOCTOU race, which is what the ``exists-then-read`` gate names. But the damage
in a credential loader is that ``exists()`` answers a different question from
``open()``: a file that is present and NOT READABLE -- wrong mode, wrong owner,
a locked file on Windows -- takes the ``exists`` branch, raises inside the
``with``, and the enclosing ``except Exception`` swallows it. The loader then
moves on to the next candidate path.

So the failure is silent and it is an identity failure: the machine's real
credentials are skipped, a lower-precedence file answers instead, and the agent
authenticates as somebody else with nothing in the log to say so.

The fix distinguishes the two: only ``FileNotFoundError`` means absent;
anything else is logged and the path is still skipped, but visibly.

These tests exercise the loop directly by pointing the candidate paths at a
tmp_path, so they run on any OS despite the function's Windows name.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from core import auth  # tests/conftest.py puts apps/backend on sys.path

_REAL_IDENTITY = "sk-ant-oat01-SENTINEL-real-machine-identity"
_OTHER_IDENTITY = "sk-ant-oat01-SENTINEL-lower-precedence-file"


def _point_loader_at(monkeypatch: pytest.MonkeyPatch, paths: list[Path]) -> None:
    """Make the loader walk `paths` in order, whatever platform we are on."""
    queue = iter([str(p) for p in paths])
    monkeypatch.setattr(auth.os.path, "expandvars", lambda _s: next(queue))


def test_a_readable_credentials_file_is_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path the fix must not disturb."""
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"claudeAiOauth": {"accessToken": _REAL_IDENTITY}}))
    _point_loader_at(
        monkeypatch,
        [good, tmp_path / "b.json", tmp_path / "c.json", tmp_path / "d.json"],
    )

    assert auth._get_token_from_windows_credential_files() == _REAL_IDENTITY


def test_an_absent_file_falls_through_to_the_next_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent really is absent: keep walking, no noise."""
    second = tmp_path / "b.json"
    second.write_text(json.dumps({"claudeAiOauth": {"accessToken": _REAL_IDENTITY}}))
    _point_loader_at(
        monkeypatch,
        [tmp_path / "missing.json", second, tmp_path / "c.json", tmp_path / "d.json"],
    )

    assert auth._get_token_from_windows_credential_files() == _REAL_IDENTITY


def test_an_unreadable_file_is_reported_not_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bug. A present-but-unreadable file must not look like an absent one.

    Without the fix this test still returns ``_OTHER_IDENTITY`` -- the assertion
    that fails is the one on the log, because the old code had no way to say
    "I found your credentials and could not read them". That silence is the
    defect; the wrong identity is its consequence.
    """
    unreadable = tmp_path / "a.json"
    unreadable.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": _REAL_IDENTITY}})
    )
    unreadable.chmod(0o000)
    try:
        unreadable.read_text()
    except PermissionError:
        pass
    else:  # running as root, where mode 000 is still readable
        pytest.skip("running as root: mode 000 is still readable")

    lower_precedence = tmp_path / "b.json"
    lower_precedence.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": _OTHER_IDENTITY}})
    )
    _point_loader_at(
        monkeypatch,
        [unreadable, lower_precedence, tmp_path / "c.json", tmp_path / "d.json"],
    )

    with caplog.at_level(logging.WARNING, logger="core.auth"):
        auth._get_token_from_windows_credential_files()

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not be read" in messages, (
        "an unreadable credentials file was skipped silently; it is "
        "indistinguishable from an absent one"
    )
    # The token itself must never reach the log.
    assert _REAL_IDENTITY not in messages
    unreadable.chmod(0o600)
