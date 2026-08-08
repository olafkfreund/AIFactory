"""A moved file must be measured against its pre-rename self (#1218).

`base_count` looks the file up in a worktree of the base by its HEAD path. A
renamed file is not there under that name, so the baseline read as empty and
every violation the file already carried was scored as net-new: `git mv` of a
1570-line legacy module reported `0 -> 167` while changing nothing but its
import prefixes.

That is a ratchet punishing the cleanup it exists to encourage -- and the only
ways to satisfy it were to abandon the move or to rewrite a legacy file in the
same commit, which is exactly the large unrelated diff the gate wants to
prevent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cq_ratchet  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_caches():
    """`_base_worktree` is cached per process and keyed only on the base ref.

    These tests run against a throwaway repo, so a cached worktree must not
    survive into (or arrive from) a test measuring the real one.
    """
    cq_ratchet._base_worktree.cache_clear()
    cq_ratchet._rename_sources.cache_clear()
    yield
    cq_ratchet._base_worktree.cache_clear()
    cq_ratchet._rename_sources.cache_clear()


def _git(repo: Path, *args: str) -> str:
    # Strip the git-hook exports: run under `git commit`, GIT_DIR/GIT_INDEX_FILE
    # point at the REAL repo and this fixture would operate on it.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX")
    }
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout


def _repo_with_a_rename(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "old.py").write_text("import os\nprint(os)\n", encoding="utf-8")
    _git(repo, "add", "old.py")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "mv", "old.py", "new.py")
    return repo


def _counter_of(counts: dict[str, dict[str, int]]):
    """A stand-in checker: violations keyed by the file CONTENT it is handed."""

    def counter(_config: str, file_on_disk: str, _repo_path: str) -> Counter[str]:
        return Counter(counts.get(Path(file_on_disk).name, {}))

    return counter


def test_a_renamed_file_is_measured_against_its_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: the baseline must come from `old.py`, not from nothing."""
    monkeypatch.chdir(_repo_with_a_rename(tmp_path))

    before = cq_ratchet.base_count(
        "HEAD",
        _counter_of({"old.py": {"T201": 1}}),
        "unused.toml",
        "new.py",
        staged=True,
    )
    assert before == Counter({"T201": 1}), (
        "the pre-rename file carried T201; reading an empty baseline here is what "
        "turns a pure `git mv` into a reported regression"
    )


def test_a_genuinely_new_file_still_has_an_empty_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename lookup must not invent a baseline for a file that is new."""
    repo = _repo_with_a_rename(tmp_path)
    (repo / "brand_new.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", "brand_new.py")
    monkeypatch.chdir(repo)

    before = cq_ratchet.base_count(
        "HEAD",
        _counter_of({"old.py": {"T201": 1}}),
        "unused.toml",
        "brand_new.py",
        staged=True,
    )
    assert before == Counter()
