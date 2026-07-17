#!/usr/bin/env python3
"""
Concurrent base-repo worktree safety (RFC-0016 #669)
====================================================

Admission control (#672) lets multiple independent builds run concurrently in
one pod, each with its own WorktreeManager against the SAME base repo .git.
`git worktree add` / `branch -D` / `worktree remove` mutate that shared .git
and take git's index.lock; the per-build asyncio locks do NOT serialize across
builds. These tests prove the new cross-process file lock serializes those
mutations so concurrent worktree-add calls don't corrupt / race the shared repo.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

# Pull every symbol off the SAME module object (conftest pre-mocks the heavy
# SDK deps so importing core.worktree directly is safe). Importing WorktreeError
# from a different module instance would make pytest.raises miss the raised one.
from core.worktree import (
    WorktreeError,
    WorktreeManager,
    _BaseRepoGitLock,
    _git_env,
)


def _is_valid_worktree(manager: WorktreeManager, spec_name: str) -> bool:
    """A worktree is valid iff it exists with a .git file on the right branch."""
    info = manager.get_worktree_info(spec_name)
    return info is not None and info.branch == manager.get_branch_name(spec_name)


class TestConcurrentWorktreeAdd:
    """Two concurrent builds adding worktrees against one base repo."""

    def test_concurrent_create_worktree_all_succeed(self, temp_git_repo: Path):
        """N concurrent create_worktree calls each produce a valid worktree.

        Each build constructs its OWN manager (mirrors separate run.py Job
        processes), so only the cross-process file lock — not any in-manager
        asyncio lock — can serialize them.

        WITHOUT the lock, the concurrent `git worktree add` calls hit
        index.lock contention ("Unable to create '.../index.lock': File
        exists") and some worktrees fail to materialize / the registry is left
        inconsistent. WITH the lock they queue and all succeed.

        A worktree-add is expected to succeed FIRST TIME here: it is serialized
        by the lock and touches only this test's own temp repo. There is no
        retry — an "index file open failed" here was never an under-load
        hiccup but a deterministic ambient-env bug (#819), and retrying only
        hid it. See TestAmbientGitEnv below.
        """
        spec_names = [f"spec-{i:02d}" for i in range(4)]
        errors: list[Exception] = []
        barrier = threading.Barrier(len(spec_names))

        # The lock contention signature we must NOT see (would mean two builds
        # raced the shared index despite the lock).
        contention_markers = ("index.lock", "unable to create", "cannot lock ref")

        def worker(spec_name: str) -> None:
            try:
                mgr = WorktreeManager(temp_git_repo, base_branch="main")
                barrier.wait()  # maximize the race on `git worktree add`
                mgr.create_worktree(spec_name)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(s,)) for s in spec_names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # No error may carry a lock-contention signature (the bug this fixes).
        for exc in errors:
            assert not any(m in str(exc).lower() for m in contention_markers), (
                f"concurrent worktree add hit index/ref contention: {exc}"
            )
        assert not errors, f"concurrent create_worktree raised: {errors}"

        # Every worktree must be a valid, distinct checkout on its own branch.
        verify_mgr = WorktreeManager(temp_git_repo, base_branch="main")
        for spec_name in spec_names:
            assert _is_valid_worktree(verify_mgr, spec_name), (
                f"worktree for {spec_name} is missing/corrupt after concurrent add"
            )

        # The shared repo's worktree registry must not be corrupt: `git worktree
        # list` must succeed and list every worktree we created.
        listed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
        )
        assert listed.returncode == 0, f"git worktree list failed: {listed.stderr}"
        for spec_name in spec_names:
            assert spec_name in listed.stdout

    def test_lock_actually_serializes(self, temp_git_repo: Path):
        """The base-repo lock is mutually exclusive across holders.

        Two managers (separate fds, as separate build processes would have) must
        never hold the lock simultaneously — the second waits for the first.
        """
        mgr_a = WorktreeManager(temp_git_repo, base_branch="main")
        mgr_b = WorktreeManager(temp_git_repo, base_branch="main")

        # Both resolve to the SAME sentinel (shared .git) — the rendezvous point.
        assert mgr_a._git_lock_path == mgr_b._git_lock_path

        overlap = {"value": False}
        in_critical = threading.Event()
        release = threading.Event()

        def hold_first() -> None:
            with mgr_a._base_repo_git_lock():
                in_critical.set()
                release.wait(timeout=5)

        t = threading.Thread(target=hold_first)
        t.start()
        assert in_critical.wait(timeout=5), (
            "first holder never entered critical section"
        )

        # While the first holder is inside, the second must NOT acquire.
        acquired_second = {"value": False}

        def try_second() -> None:
            # Expected to time out while the first holder is inside; swallow it
            # here so it doesn't surface as an unhandled-thread warning.
            try:
                with _BaseRepoGitLock(mgr_b._git_lock_path, timeout=0.3):
                    acquired_second["value"] = True
                    if in_critical.is_set() and not release.is_set():
                        overlap["value"] = True
            except WorktreeError:
                pass

        t2 = threading.Thread(target=try_second)
        t2.start()
        t2.join(timeout=5)

        # Second acquisition timed out (lock held) → it must have raised, so it
        # never set acquired_second / overlap.
        assert not overlap["value"], "lock allowed two simultaneous holders"
        assert not acquired_second["value"], "second holder acquired while first held"

        # Releasing the first lets a fresh attempt succeed.
        release.set()
        t.join(timeout=5)
        with _BaseRepoGitLock(mgr_b._git_lock_path, timeout=5):
            pass

    def test_lock_timeout_raises_worktree_error(self, temp_git_repo: Path):
        """A wedged holder makes a waiting acquire fail with a clear, bounded error."""
        mgr = WorktreeManager(temp_git_repo, base_branch="main")
        with _BaseRepoGitLock(mgr._git_lock_path, timeout=5):
            start = time.monotonic()
            with pytest.raises(WorktreeError, match="base-repo git lock"):
                with _BaseRepoGitLock(mgr._git_lock_path, timeout=0.2):
                    pass
            assert time.monotonic() - start < 2, "timeout was not bounded"

    def test_lock_sentinel_lives_in_shared_git_dir(self, temp_git_repo: Path):
        """Sentinel resolves into the git common dir (shared by all worktrees)."""
        mgr = WorktreeManager(temp_git_repo, base_branch="main")
        assert mgr._git_lock_path.name == "aifactory-worktree.lock"
        assert mgr._git_lock_path.parent == (temp_git_repo / ".git").resolve()


class TestAmbientGitEnv:
    """Ambient GIT_* env vars must not aim our git commands at another repo (#819).

    Git exports these into hook environments: during a `pre-commit` hook it sets
    GIT_INDEX_FILE=.git/index — a RELATIVE path. Anything the hook runs (the test
    suite, or AIFactory itself) inherits it, and every git child re-resolves that
    relative path against its OWN cwd. `git worktree add` checks out with cwd set
    to the new worktree, where `.git` is a FILE, so `.git/index` hits ENOTDIR:
    "fatal: .git/index: index file open failed: Not a directory".

    This is deterministic, not a race: it presents as a "flake" only because the
    suite passes when run straight from a shell and fails when run via the hook.
    """

    def test_create_worktree_ignores_ambient_git_index_file(
        self, temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative GIT_INDEX_FILE (as git sets for hooks) must not break add."""
        monkeypatch.setenv("GIT_INDEX_FILE", ".git/index")

        mgr = WorktreeManager(temp_git_repo, base_branch="main")
        mgr.create_worktree("spec-env")

        assert _is_valid_worktree(mgr, "spec-env")

    def test_create_worktree_ignores_ambient_git_dir(
        self, temp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GIT_DIR pointing at an unrelated repo must not hijack our commands."""
        other = tmp_path / "other-repo"
        other.mkdir()
        subprocess.run(["git", "init"], cwd=other, capture_output=True, check=True)
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))

        mgr = WorktreeManager(temp_git_repo, base_branch="main")
        mgr.create_worktree("spec-gitdir")

        # The worktree must exist in OUR repo, not the GIT_DIR-designated one.
        assert _is_valid_worktree(mgr, "spec-gitdir")
        assert not (other / ".aifactory").exists()

    def test_git_env_strips_pinning_vars_but_keeps_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_git_env drops only the repo-pinning vars, preserving PATH etc."""
        monkeypatch.setenv("GIT_INDEX_FILE", ".git/index")
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")

        env = _git_env()

        assert "GIT_INDEX_FILE" not in env
        # Author/committer identity and general env must survive.
        assert env["GIT_AUTHOR_NAME"] == "Test User"
        assert "PATH" in env


class TestCrossProcessLock:
    """Prove the lock holds across real OS processes, not just threads."""

    def test_two_processes_do_not_overlap(self, temp_git_repo: Path):
        """fcntl.flock contends across processes: the child cannot grab a held lock."""
        mgr = WorktreeManager(temp_git_repo, base_branch="main")
        lock_path = str(mgr._git_lock_path)

        # Child tries to take the lock (timeout 0.2s) and reports the outcome.
        child_src = (
            "import sys, os\n"
            "sys.path.insert(0, os.environ['BACKEND_PATH'])\n"
            # Import the shim first: it registers core.worktree in sys.modules
            # without dragging in core/__init__'s heavy deps.
            "import worktree  # noqa: F401\n"
            "from core.worktree import _BaseRepoGitLock, WorktreeError\n"
            "try:\n"
            "    with _BaseRepoGitLock(os.environ['LOCK_PATH'], timeout=0.2):\n"
            "        print('ACQUIRED')\n"
            "except WorktreeError:\n"
            "    print('BLOCKED')\n"
        )
        env = dict(os.environ)
        backend_path = str(Path(__file__).parent.parent / "apps" / "backend")
        env["BACKEND_PATH"] = backend_path
        env["LOCK_PATH"] = lock_path

        with _BaseRepoGitLock(mgr._git_lock_path, timeout=5):
            blocked = subprocess.run(
                ["python", "-c", child_src],
                capture_output=True,
                text=True,
                env=env,
            )
        assert blocked.stdout.strip() == "BLOCKED", (
            f"child acquired a held cross-process lock: "
            f"out={blocked.stdout!r} err={blocked.stderr!r}"
        )

        # Lock released: a fresh child must now acquire.
        free = subprocess.run(
            ["python", "-c", child_src],
            capture_output=True,
            text=True,
            env=env,
        )
        assert free.stdout.strip() == "ACQUIRED", (
            f"child could not acquire a free cross-process lock: "
            f"out={free.stdout!r} err={free.stderr!r}"
        )
