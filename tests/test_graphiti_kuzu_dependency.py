"""#1032 — is `kuzu` actually missing, or does `real_ladybug` already cover it?

The issue's supporting fact was: "`kuzu` is not installed in the running image
(`real_ladybug` and `graphiti-core` are in `requirements.txt`; `kuzu` imports
fail in-pod)." True as stated, but incomplete: `real_ladybug` has been in
`apps/backend/requirements.txt` since #796 — well before this issue was filed
— and the monkeypatch that makes it stand in for `kuzu`
(`integrations.graphiti.queries_pkg.client._apply_ladybug_monkeypatch`) already
existed too. The dependency gap the issue flagged was never actually blocking;
it was masked by the broken import (#1032 checkbox 1, fixed in #1257) raising
before the code ever got far enough to touch `kuzu`/`real_ladybug` at all.

This asserts the two facts separately so neither can be assumed again:

1. `kuzu` is genuinely absent (so the issue's observation was accurate).
2. With the import fixed (#1257), enabling Graphiti reaches a real,
   *enabled* ``GraphitiMemory`` instance backed by `real_ladybug` — the
   "or confirm real_ladybug supersedes it" branch of #1032's kuzu checkbox.

No network / API key required: `GraphitiConfig.is_valid()` needs only
``GRAPHITI_ENABLED=true`` (see ``test_graphiti.py::TestIsGraphitiEnabled``).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def test_native_kuzu_is_genuinely_absent() -> None:
    """Confirms the issue's premise — not installed, not a stale assumption."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("kuzu")


def test_real_ladybug_is_installed() -> None:
    """The dependency the monkeypatch needs is present (has been since #796)."""
    importlib.import_module("real_ladybug")  # raises if missing


def test_graphiti_reaches_an_enabled_instance_via_real_ladybug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: enabled Graphiti -> real GraphitiMemory, kuzu never touched.

    Regression guard for the exact question #1032 leaves open on the kuzu
    checkbox: without the monkeypatch working, this would fall through
    ``get_graphiti_memory``'s ``except ImportError`` and return None.
    """
    monkeypatch.setenv("GRAPHITI_ENABLED", "true")

    from memory.graphiti_helpers import get_graphiti_memory

    memory = get_graphiti_memory(tmp_path)

    assert memory is not None, (
        "Graphiti did not reach a usable instance — kuzu really is a blocker "
        "now (dependencies may have drifted); re-open #1032's kuzu checkbox"
    )
    assert memory.is_enabled is True

    # The DB connection (and its kuzu monkeypatch) is lazy — GraphitiMemory
    # construction alone doesn't touch it. Exercise the monkeypatch directly,
    # the same function the client calls right before it would otherwise fail
    # with "Neither LadybugDB nor kuzu installed".
    from integrations.graphiti.queries_pkg.client import _apply_ladybug_monkeypatch

    assert _apply_ladybug_monkeypatch() is True
    assert sys.modules["kuzu"] is importlib.import_module("real_ladybug")
