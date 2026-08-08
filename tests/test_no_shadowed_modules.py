"""No module may be shadowed by a same-named package beside it (#1218).

When `foo.py` and `foo/` sit in the same directory, Python resolves the PACKAGE
and ignores the module. Two things follow, and both were true in this repo:

* The module is unreachable. Seven files here documented themselves as facades
  "so existing imports continue to work" and none of them could ever run; each
  body (`from .command_registry import ...`, `from security import *`, ...)
  would have imported the package that shadows it if it ever had.

* Static tooling cannot see past it. A whole-package mypy measurement over
  apps/backend exited 2 having checked NOTHING:

      core/workspace/__init__.py: error: Duplicate module named
        "backend.core.workspace" (also at "core/workspace.py")
      Found 1 error in 1 file (errors prevented further checking)

  "mypy exits 2 having checked nothing" is the shape of defect that keeps a gate
  dark without anyone noticing. The per-file ratchet is unaffected (it checks one
  file at a time), so nothing was ungated — but every whole-package sweep,
  report or measurement was blocked.

The allowlist below only ever shrinks. Adding to it is how #1218 happens again.

Refs AIFactory#1218, TFactory#999, PFactory#477.
"""

from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "apps" / "backend"

# Empty, and it stays empty. `test_the_allowlist_does_not_outlive_its_entries`
# fails on any entry that is no longer shadowed, so this cannot quietly rot into
# a permanent exemption list.
_KNOWN_SHADOWED: set[str] = set()

# The seven facades deleted in #1218. Every one was unreachable AND
# self-importing: the name it imported resolved to the package shadowing it.
_DELETED_FACADES = (
    ("security.py", "security"),
    ("project/command_registry.py", "project/command_registry"),
    ("merge/ai_resolver.py", "merge/ai_resolver"),
    ("merge/auto_merger.py", "merge/auto_merger"),
    ("merge/file_evolution.py", "merge/file_evolution"),
    ("spec/phases.py", "spec/phases"),
    ("spec/pipeline.py", "spec/pipeline"),
)


def _shadowed_pairs() -> set[str]:
    """Every `x.py` that sits beside a package directory `x/`."""
    return {
        str(package.relative_to(_BACKEND))
        for package in _BACKEND.rglob("*")
        if package.is_dir()
        and (package / "__init__.py").is_file()
        and package.with_suffix(".py").is_file()
    }


def test_no_new_shadowed_modules() -> None:
    """A same-named module and package in one directory is never intentional."""
    unexpected = _shadowed_pairs() - _KNOWN_SHADOWED
    assert not unexpected, (
        f"module(s) shadowed by a same-named package: {sorted(unexpected)}. "
        f"Python resolves the package, so the .py file is unreachable, and a "
        f"whole-package mypy run aborts with 'Duplicate module named ...' "
        f"having checked nothing. Delete the module or fold it into the package."
    )


def test_the_allowlist_does_not_outlive_its_entries() -> None:
    """A stale allowlist entry silently re-permits the pattern it was granted for."""
    stale = _KNOWN_SHADOWED - _shadowed_pairs()
    assert not stale, (
        f"_KNOWN_SHADOWED lists {sorted(stale)}, which is no longer shadowed. "
        f"Remove the entry — an allowlist that outlives its cause is how the "
        f"next one gets waved through."
    )


def test_the_deleted_facades_stay_deleted() -> None:
    """All seven were provably unreachable, and self-importing if reached.

    `project/command_registry.py` did `from .command_registry import ...` — the
    package that shadows it. `security.py` did `from security import *`, which
    inside apps/backend is the `security/` package, so it would have imported
    itself. Every importer in the fleet already resolves to the package.
    """
    for module, package in _DELETED_FACADES:
        assert not (_BACKEND / module).exists(), f"{module} came back"
        assert (_BACKEND / package / "__init__.py").is_file()


def test_the_packages_still_serve_the_imports_the_facades_claimed_to() -> None:
    """Deleting them must change nothing for callers — that was the whole claim."""
    import sys

    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    from merge.ai_resolver import AIResolver
    from merge.auto_merger import AutoMerger
    from merge.file_evolution import FileEvolutionTracker
    from project.command_registry import BASE_COMMANDS, VALIDATED_COMMANDS
    from security import bash_security_hook
    from spec.phases import PhaseExecutor
    from spec.pipeline import SpecOrchestrator

    assert BASE_COMMANDS and VALIDATED_COMMANDS
    assert callable(bash_security_hook)
    for cls in (
        AIResolver,
        AutoMerger,
        FileEvolutionTracker,
        PhaseExecutor,
        SpecOrchestrator,
    ):
        assert isinstance(cls, type)


def test_the_merge_logic_is_reachable_by_its_real_name() -> None:
    """`core/workspace.py` was executed by PATH under the name `workspace_module`.

    That gave 1570 lines of merge logic a second module identity no static tool
    could follow, and left it importable only through a shim. It now lives inside
    the package, so `__module__` is a name that actually resolves — which is the
    difference between "loaded" and "reachable".
    """
    import importlib
    import sys

    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    workspace = importlib.import_module("core.workspace")

    assert workspace.merge_existing_build.__module__ == "core.workspace.merge"
    assert (
        importlib.import_module("core.workspace.merge")
        is sys.modules["core.workspace.merge"]
    )
    assert "workspace_module" not in sys.modules, (
        "the importlib shim is back: the merge logic is loaded under a second "
        "identity, so anything comparing types across the two paths sees "
        "different objects"
    )
