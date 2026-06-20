"""Build-layout guard: catch the root-vs-src import-shadowing defect (#601).

A generated build shipped an internally inconsistent Python layout: the real app
lived in a repo-root module (``app.py``), but ``pyproject.toml`` put ``src`` on
the import path (``[tool.pytest.ini_options] pythonpath = ["src"]`` and/or
``[tool.setuptools.packages.find] where = ["src"]``) while a near-empty
``src/app/`` package also existed. pytest then prepends ``src/`` so ``import app``
resolves to the empty ``src/app`` package and **shadows** the root ``app.py`` —
every ``from app import app`` fails with ``ImportError``, breaking the build's
own tests and TFactory's generated ones alike.

This is a pure, deterministic detector run by the trailing gates: if it fires,
the build is failed loud (into ``GATE_FAILURES.md``) so the QA/fix loop resolves
the layout instead of shipping a build whose tests can never import the app.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["detect_import_layout_conflicts"]


def _src_on_import_path(pyproject: dict) -> bool:
    """True if pyproject puts ``src`` on the import path (pytest or setuptools)."""
    tool = pyproject.get("tool") or {}
    pytest_paths = ((tool.get("pytest") or {}).get("ini_options") or {}).get(
        "pythonpath"
    ) or []
    if isinstance(pytest_paths, str):
        pytest_paths = [pytest_paths]
    if any(str(p).strip().strip("/.") == "src" for p in pytest_paths):
        return True
    where = (
        ((tool.get("setuptools") or {}).get("packages") or {}).get("find") or {}
    ).get("where") or []
    if isinstance(where, str):
        where = [where]
    return any(str(w).strip().strip("/.") == "src" for w in where)


def detect_import_layout_conflicts(project_dir: Path | str) -> list[str]:
    """Return human-readable layout conflicts for ``project_dir`` (empty = clean).

    Detects the #601 signature: ``src`` is on the import path AND a top-level
    root module ``<name>.py`` is shadowed by a same-named ``src/<name>/`` package
    — so ``import <name>`` resolves to the (often skeleton) src package, not the
    root module where the app actually lives. Pure + never raises; an unreadable
    or tomllib-less environment yields no findings (the guard simply no-ops).
    """
    root = Path(project_dir)
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        return []
    try:
        import tomllib

        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — best-effort; no tomllib / malformed → no-op
        return []

    if not _src_on_import_path(pyproject):
        return []

    src = root / "src"
    if not src.is_dir():
        return []

    conflicts: list[str] = []
    for module in sorted(root.glob("*.py")):
        name = module.stem
        pkg_init = src / name / "__init__.py"
        if pkg_init.is_file():
            conflicts.append(
                f"import shadowing: root './{module.name}' is shadowed by the "
                f"'src/{name}/' package because pyproject puts 'src' on the import "
                f"path — `import {name}` resolves to src/{name}/ (often a skeleton), "
                f"not ./{module.name}. Put the app under src/ OR drop 'src' from the "
                f"import path OR remove the empty src/{name}/ package (#601)."
            )
    return conflicts
