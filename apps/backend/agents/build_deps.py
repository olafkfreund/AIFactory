"""Build-deps guard: catch undeclared test-only dependencies (#611f).

The taskboard build's generated ``requirements.txt`` omitted ``httpx`` even
though the tests use Starlette/FastAPI's ``TestClient``, which imports ``httpx``
at runtime — so the tests ImportError'd despite the app being fine. The build
passed its own (non-running) checks and shipped a suite that can't execute.

This is a pure, deterministic guard run by the trailing gates: it maps a handful
of well-known APIs to the third-party package they pull in (``TestClient`` →
``httpx``), and if the project's test files use one whose package is **not
declared** in ``requirements.txt`` / ``pyproject`` deps, it fails the build loud
(into ``GATE_FAILURES.md``) so the fix loop adds the dep — instead of shipping a
suite that can't import.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

__all__ = ["detect_undeclared_test_deps"]

# Marker in source -> the third-party distribution it requires at runtime. Kept
# deliberately small + high-signal (the demo gap + its closest cousins); extend
# as new transitive-import traps surface.
_API_REQUIRES: tuple[tuple[str, str], ...] = (
    ("from fastapi.testclient import", "httpx"),  # Starlette TestClient needs httpx
    ("from starlette.testclient import", "httpx"),
    ("import httpx", "httpx"),
    ("from httpx import", "httpx"),
)


def _declared_deps_blob(root: Path) -> str:
    """All declared-dependency text (requirements*.txt + pyproject), lowercased."""
    parts: list[str] = []
    for req in sorted(root.glob("requirements*.txt")):
        # ponytail: unreadable/missing req file just contributes nothing to the blob
        with contextlib.suppress(OSError):
            parts.append(req.read_text(encoding="utf-8"))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        with contextlib.suppress(OSError):
            parts.append(pyproject.read_text(encoding="utf-8"))
    return "\n".join(parts).lower()


def _dep_declared(dist: str, blob: str) -> bool:
    """True if ``dist`` appears as a declared dependency token in ``blob``."""
    # Match the distribution name as a whole token (so `httpx` matches but a
    # substring like `httpxy` does not), tolerant of version specifiers/extras.
    return (
        re.search(rf"(^|[^a-z0-9_-]){re.escape(dist)}([^a-z0-9_-]|$)", blob) is not None
    )


def detect_undeclared_test_deps(project_dir: Path | str) -> list[str]:
    """Return undeclared test-dependency problems for ``project_dir`` (empty=clean).

    Scans ``test_*.py`` / ``*_test.py`` (and a ``tests/`` tree) for the markers in
    :data:`_API_REQUIRES`; for each hit whose distribution isn't declared in the
    project's requirements/pyproject, returns a human-readable finding. Pure +
    never raises.
    """
    root = Path(project_dir)
    test_files: list[Path] = []
    test_files.extend(root.glob("test_*.py"))
    test_files.extend(root.glob("*_test.py"))
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        test_files.extend(tests_dir.rglob("*.py"))
    if not test_files:
        return []

    blob = _declared_deps_blob(root)
    findings: list[str] = []
    flagged: set[str] = set()
    for tf in sorted(set(test_files)):
        try:
            text = tf.read_text(encoding="utf-8")
        except OSError:
            continue
        for marker, dist in _API_REQUIRES:
            if marker in text and dist not in flagged and not _dep_declared(dist, blob):
                flagged.add(dist)
                rel = tf.relative_to(root) if tf.is_relative_to(root) else tf.name
                findings.append(
                    f"undeclared test dependency: {rel} uses '{marker.strip()} …' "
                    f"which requires '{dist}', but '{dist}' is not declared in "
                    f"requirements*.txt / pyproject — the test suite will "
                    f"ImportError at runtime. Add '{dist}' to the project deps (#611f)."
                )
    return findings
