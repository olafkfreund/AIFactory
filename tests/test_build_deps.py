"""Tests for the undeclared test-dependency build guard (#611f)."""

from __future__ import annotations

from pathlib import Path

from agents.build_deps import detect_undeclared_test_deps


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


_TESTCLIENT = "from fastapi.testclient import TestClient\n\ndef test_root():\n    pass\n"


def test_flags_testclient_without_httpx(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_app.py", _TESTCLIENT)
    _write(tmp_path / "requirements.txt", "fastapi\nuvicorn\n")  # no httpx
    findings = detect_undeclared_test_deps(tmp_path)
    assert len(findings) == 1 and "httpx" in findings[0]


def test_clean_when_httpx_declared(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_app.py", _TESTCLIENT)
    _write(tmp_path / "requirements.txt", "fastapi\nuvicorn\nhttpx>=0.27\n")
    assert detect_undeclared_test_deps(tmp_path) == []


def test_clean_when_httpx_declared_in_pyproject(tmp_path: Path) -> None:
    _write(tmp_path / "test_app.py", _TESTCLIENT)
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "x"\ndependencies = ["fastapi", "httpx"]\n',
    )
    assert detect_undeclared_test_deps(tmp_path) == []


def test_substring_does_not_count_as_declared(tmp_path: Path) -> None:
    # a look-alike token must NOT satisfy the httpx requirement
    _write(tmp_path / "tests" / "test_app.py", _TESTCLIENT)
    _write(tmp_path / "requirements.txt", "fastapi\nhttpxy-thing\n")
    assert len(detect_undeclared_test_deps(tmp_path)) == 1


def test_no_tests_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "fastapi\n")
    assert detect_undeclared_test_deps(tmp_path) == []


def test_reported_once_per_dist(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_a.py", _TESTCLIENT)
    _write(tmp_path / "tests" / "test_b.py", "import httpx\n")
    _write(tmp_path / "requirements.txt", "fastapi\n")
    # both files need httpx, but it's a single missing-dep finding
    assert len(detect_undeclared_test_deps(tmp_path)) == 1
