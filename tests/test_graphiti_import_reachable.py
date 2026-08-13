"""Graphiti must be importable, and a broken one must be LOUD (#1032).

Two separate failures, and the second is why the first survived:

1. Four call sites did `from graphiti_memory import GraphitiMemory`. There is no
   top-level `graphiti_memory` module and nothing aliased one, so the import
   raised ModuleNotFoundError every time. The class lives at
   `integrations.graphiti.queries_pkg.graphiti` and is re-exported by
   `integrations.graphiti.memory`.

2. Every caller wrapped it in `except ImportError` and returned None, logging at
   `debug` or not at all. A configured-but-broken store was therefore
   indistinguishable from a store nobody turned on — so the graph memory sat
   unreachable for months with every signal reading normal.

This file is also the first Graphiti test in CI. `integrations/graphiti/conftest.py`
excludes the existing ones as "not unit tests", which is the other half of how
this rotted: a store with no test in the suite.

Deliberately NOT asserted here: that the graph store WORKS end to end (a real
episode write/query) or the size-gating routing decision — see
`test_graphiti_kuzu_dependency.py` for the kuzu question specifically (`kuzu`
itself is absent, but `real_ladybug` — already in `requirements.txt` since
#796 — supersedes it via an existing monkeypatch, so that is NOT a blocker).
Size-gating is still open, and needs an RFC-0014 extension this repo alone
can't land. This file asserts only that the path is reachable and that its
failure is audible, which is what makes the rest measurable.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def test_the_facade_exports_what_the_call_sites_import() -> None:
    """The names the four repointed sites ask for must exist on the facade."""
    mod = importlib.import_module("integrations.graphiti.memory")
    for name in ("GraphitiMemory", "GroupIdMode", "is_graphiti_enabled"):
        assert hasattr(mod, name), f"the facade no longer exports {name}"


def test_no_call_site_imports_the_module_that_never_existed() -> None:
    """`graphiti_memory` is not importable, so nothing may import it.

    This is the regression guard. The name reads plausible, the facade's own
    docstring recommended it for months, and every failure was swallowed — so
    the cheapest way for this to come back is someone restoring the old spelling
    from memory.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("graphiti_memory")

    offenders = [
        str(p.relative_to(_BACKEND))
        for p in _BACKEND.rglob("*.py")
        if "from graphiti_memory import" in p.read_text(errors="ignore")
    ]
    assert offenders == [], (
        "these import a module that does not exist, and their `except ImportError` "
        f"will swallow it exactly as #1032 describes: {offenders}"
    )


def test_a_configured_but_broken_graphiti_is_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Enabled + import fails must log at ERROR, not vanish.

    The silence is the defect this test exists for: at `debug`, a broken store
    and an unconfigured one look identical in the logs.
    """
    helpers = importlib.import_module("memory.graphiti_helpers")
    monkeypatch.setattr(helpers, "is_graphiti_memory_enabled", lambda: True)

    real_import = importlib.__import__

    def _break_graphiti(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("integrations.graphiti"):
            raise ImportError("simulated: graphiti packages missing")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.__import__", _break_graphiti)

    with caplog.at_level(logging.ERROR):
        result = helpers.get_graphiti_memory(tmp_path)

    assert result is None, "the fallback itself must still happen"
    assert caplog.records, (
        "a configured-but-broken Graphiti fell back SILENTLY — that is the "
        "defect #1032 records, not a detail of it"
    )
    assert any(r.levelno >= logging.ERROR for r in caplog.records), [
        r.levelname for r in caplog.records
    ]


def test_graphiti_off_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Not configured is not a failure, and must not shout.

    Without this the fix is just a different bug: every run of every project
    that has never enabled Graphiti would log an error.
    """
    helpers = importlib.import_module("memory.graphiti_helpers")
    monkeypatch.setattr(helpers, "is_graphiti_memory_enabled", lambda: False)

    with caplog.at_level(logging.WARNING):
        assert helpers.get_graphiti_memory(tmp_path) is None
    assert caplog.records == [], [r.message for r in caplog.records]
