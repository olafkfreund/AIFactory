#!/usr/bin/env python3
"""Regression check for cq_ratchet.base_count's temp-file naming.

Bug: base_count() used to write the PR-base version of a file into
tempfile.NamedTemporaryFile(suffix=...), which produces a random-PREFIXED
name (e.g. /tmp/tmpXXXXXX.py). That name never matches ruff's
per-file-ignores globs (**/test_*.py, **/tests/**), so every test file's
BASE count was computed under the non-test strict bar (S101 asserts,
PLR2004 magic values, ...) while its HEAD count (checked out at its real
path) correctly got the exemption. before=0 (wrong) vs after=1 (correct)
looks like a regression and fails the ratchet on unrelated PRs.

Fix: write the base blob under its REAL basename inside a fresh
tempfile.mkdtemp() dir, so the path ends in the real filename and
per-file-ignores match on both sides. This proves the fix directly against
base_count(), the only place cq_ratchet still writes a temp file (the
on-disk HEAD path was never the problem).

Run: python3 scripts/test_cq_ratchet.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cq_ratchet

RUFF_CONFIG = str(Path(__file__).parents[1] / "standards" / "ruff.toml")
ASSERT_SOURCE = "def f(x):\n    assert x\n    return x\n"


def _emit(message: str) -> None:
    # This is a standalone self-check script; its stdout report IS its
    # purpose (mirrors the same sink exemption in ratchet_lint.py).
    print(message)  # noqa: T201


def _ruff_counter(config: str, file_on_disk: str) -> int:
    return cq_ratchet._ruff_count("ruff", config, file_on_disk)


def _git(repo: Path, *args: str) -> None:
    # git is a constant, trusted executable; args are fixed test fixture data.
    subprocess.run(  # noqa: S603
        [shutil.which("git") or "git", *args], cwd=repo, check=True, capture_output=True
    )


def main() -> None:
    if shutil.which("ruff") is None:
        _emit("SKIP: ruff not on PATH")
        return

    repo = Path(tempfile.mkdtemp())
    try:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "test")
        (repo / "test_sample.py").write_text(ASSERT_SOURCE)
        (repo / "sample.py").write_text(ASSERT_SOURCE)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")

        cwd = Path.cwd()
        try:
            os.chdir(repo)
            test_s101 = cq_ratchet.base_count(
                "HEAD", _ruff_counter, RUFF_CONFIG, "test_sample.py"
            )
            nontest_s101 = cq_ratchet.base_count(
                "HEAD", _ruff_counter, RUFF_CONFIG, "sample.py"
            )
        finally:
            os.chdir(cwd)

        assert test_s101 == 0, (
            f"expected 0 ruff findings for the base version of a test-named "
            f"file (S101 exempt via **/test_*.py), got {test_s101}"
        )
        assert nontest_s101 >= 1, (
            f"expected >=1 ruff finding (S101) for the base version of a "
            f"non-test-named file, got {nontest_s101}"
        )
        _emit("PASS: base_count() honours per-file-ignores for test-named files")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    main()
