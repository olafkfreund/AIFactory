"""base and head must be measured under the SAME rules (Factory#510).

`cq_ratchet` compares a file's violation count on the PR base against its count
at head. The base version is checked out into a git worktree under /tmp, and
ruff relativises a path against the project root before matching
per-file-ignores -- a path OUTSIDE that root falls back to matching the BASENAME
only. So `**/test_*.py` and `**/*_test.py` matched the worktree copy, and
`**/tests/**` could never match it at all, while the head file at its real path
matched all three.

The two sides of a no-regression comparison were therefore judged by different
rules, and unlike the too-strict failure the other services saw, this one made
the gate BLIND. The base came back inflated by every S101/PLR2004 the carve-out
would have exempted, so a file could absorb exactly that many genuine NEW
violations and still compare clean. Measured on
apps/web-server/tests/verify_file_based_endpoints.py before the fix: head 56,
base 60 -- four free violations.

The fix decouples content from identity: the base source still comes from the
worktree (which is why the worktree exists at all -- see `_base_worktree`),
while `--stdin-filename` hands ruff the real repo-relative path, identical on
both sides.

This file replaces scripts/test_cq_ratchet.py, which covered the earlier,
narrower version of this bug (a random temp PREFIX) and asserted the fix was to
write the base blob under its real BASENAME -- a claim Factory#510 disproves for
the `**/tests/**` carve-out. It also lived outside the collected test tree, so
nothing ran it. Its two cases are the first two here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cq_ratchet

_REPO = Path(__file__).resolve().parents[1]
_RUFF_CONFIG = str(_REPO / "standards" / "ruff.toml")
_ASSERT_SOURCE = "def f(x):\n    assert x\n    return x\n"


@pytest.fixture(autouse=True)
def ruff_on_path() -> Iterator[None]:
    """Make bare ``ruff`` resolvable, the way the ratchet itself needs it.

    CI runs the suite as ``apps/backend/.venv/bin/pytest``, which does NOT put
    the venv's bin on PATH — so the pinned ruff installed into that venv is
    invisible to ``shutil.which``, while `_ruff_count` is handed a bare
    ``"ruff"`` here. Venv first, then PATH.

    A hard failure, not a skip. This started life as
    ``skipif(shutil.which("ruff") is None)``, which in CI would have skipped
    every case in this file and reported green — a gate that did not run,
    reading like one that passed, which is the exact shape this line of work
    exists to stamp out.
    """
    venv_bin = Path(sys.executable).parent
    ruff_dir = str(venv_bin) if (venv_bin / "ruff").exists() else None
    if ruff_dir is None:
        found = shutil.which("ruff")
        if found is None:
            pytest.fail(
                "ruff is not in this venv or on PATH; these cases cannot measure "
                "the gate. Install the pinned ruff (tests/requirements-test.txt)."
            )
        ruff_dir = str(Path(found).parent)
    original = os.environ["PATH"]
    os.environ["PATH"] = f"{ruff_dir}{os.pathsep}{original}"
    try:
        yield
    finally:
        os.environ["PATH"] = original


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        [shutil.which("git") or "git", *args], cwd=repo, check=True, capture_output=True
    )


def _counter(config: str, file_on_disk: str, repo_path: str) -> Counter[str]:
    return cq_ratchet._ruff_count("ruff", config, file_on_disk, repo_path)


@pytest.fixture
def seeded_repo() -> Iterator[Path]:
    """A throwaway git repo with one file of each shape, committed.

    A real repo, because base_count creates a real worktree from it -- the whole
    behaviour under test is which PATH that worktree copy is judged by.
    """
    repo = Path(tempfile.mkdtemp())
    try:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "test")
        (repo / "tests").mkdir()
        for rel in ("test_sample.py", "sample.py", "tests/helpers.py"):
            (repo / rel).write_text(_ASSERT_SOURCE)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        cwd = Path.cwd()
        os.chdir(repo)
        try:
            yield repo
        finally:
            os.chdir(cwd)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# The counters are keyed by rule code since #1189; these tests are about WHICH
# PATH ruff judges a file by, which the total answers as well as the breakdown
# does, so they keep comparing totals.
def _base(path: str) -> int:
    return sum(cq_ratchet.base_count("HEAD", _counter, _RUFF_CONFIG, path).values())


def _head(path: str) -> int:
    return sum(_counter(_RUFF_CONFIG, path, path).values())


@pytest.mark.usefixtures("seeded_repo")
def test_base_exempts_a_test_named_file() -> None:
    assert _base("test_sample.py") == 0


@pytest.mark.usefixtures("seeded_repo")
def test_base_still_counts_a_production_file() -> None:
    # THE ASSERTION WITH TEETH. Exempting unconditionally passes every other
    # case here and silently drops S101 for the whole repo.
    assert _base("sample.py") >= 1


@pytest.mark.usefixtures("seeded_repo")
def test_base_exempts_a_helper_under_tests() -> None:
    """The Factory#510 case: `**/tests/**`, which a basename never matches."""
    assert _base("tests/helpers.py") == 0


@pytest.mark.usefixtures("seeded_repo")
@pytest.mark.parametrize("path", ["test_sample.py", "sample.py", "tests/helpers.py"])
def test_base_and_head_agree_for_every_shape(path: str) -> None:
    """The property that actually matters: no slack between the two sides.

    A per-shape expected count would drift with the ruff config; equality holds
    whatever the config says, and equality is the thing the ratchet's arithmetic
    depends on.
    """
    assert _base(path) == _head(path), (
        f"{path}: base and head judged by different rules"
    )
