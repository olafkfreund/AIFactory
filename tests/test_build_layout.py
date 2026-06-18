"""Tests for the root-vs-src import-shadowing build guard (#601)."""

from __future__ import annotations

from pathlib import Path

from agents.build_layout import detect_import_layout_conflicts


def _write(p: Path, text: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _conflicted_project(root: Path) -> None:
    # The exact #601 shape: real app at root app.py, empty src/app skeleton, and
    # pyproject puts src on the import path so `import app` hits the skeleton.
    _write(root / "app.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write(root / "src" / "app" / "__init__.py", '__version__ = "0.1.0"\n')
    _write(
        root / "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
    )


def test_detects_root_vs_src_shadowing(tmp_path: Path) -> None:
    _conflicted_project(tmp_path)
    conflicts = detect_import_layout_conflicts(tmp_path)
    assert len(conflicts) == 1
    assert "app.py" in conflicts[0] and "src/app/" in conflicts[0]


def test_clean_root_only_layout_has_no_conflict(tmp_path: Path) -> None:
    # Root app, no src package, no src on the path → fine.
    _write(tmp_path / "app.py", "app = object()\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    assert detect_import_layout_conflicts(tmp_path) == []


def test_clean_src_layout_has_no_conflict(tmp_path: Path) -> None:
    # App lives under src/ with no shadowing root module → fine.
    _write(tmp_path / "src" / "app" / "__init__.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write(
        tmp_path / "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    )
    assert detect_import_layout_conflicts(tmp_path) == []


def test_no_conflict_when_src_not_on_import_path(tmp_path: Path) -> None:
    # Both a root app.py and src/app exist, but src is NOT on the import path,
    # so `import app` is unambiguous (root) — not the #601 defect.
    _write(tmp_path / "app.py", "app = object()\n")
    _write(tmp_path / "src" / "app" / "__init__.py", '__version__ = "0.1.0"\n')
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    assert detect_import_layout_conflicts(tmp_path) == []


def test_missing_pyproject_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "app = object()\n")
    assert detect_import_layout_conflicts(tmp_path) == []
