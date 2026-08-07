#!/usr/bin/env python3
"""
Git Worktree Manager - Per-Spec Architecture
=============================================

Each spec gets its own worktree:
- Worktree path: .aifactory/worktrees/tasks/{spec-name}/
- Branch name: aifactory/{spec-name}

This allows:
1. Multiple specs to be worked on simultaneously
2. Each spec's changes are isolated
3. Branches persist until explicitly merged
4. Clear 1:1:1 mapping: spec → worktree → branch
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

fcntl: ModuleType | None
try:  # pragma: no cover - platform dependent
    import fcntl as _fcntl

    fcntl = _fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

logger = logging.getLogger(__name__)

# Cross-process serialization of git mutations on the SHARED base repo.
#
# Admission control (#672) lets MULTIPLE independent builds run concurrently in
# one pod, each constructing its own WorktreeManager against the SAME
# project_dir/.git. `git worktree add` / `branch -D` / `worktree remove` /
# `merge` / `commit` all mutate that shared .git (.git/worktrees, refs, the
# index) and take git's own index.lock — but each build is a separate process,
# so the per-build asyncio locks (e.g. parallel_integration's create_lock) do
# NOT serialize across builds. Two concurrent builds can then collide on
# index-lock contention or corrupt refs.
#
# We serialize those mutations on a single per-repo flock sentinel. The lock is
# bounded (timeout -> clear error) so a wedged holder can't hang a build
# forever, and the sentinel is intentionally never unlinked (deleting an flock
# target races concurrent holders — same rationale as agents/inbox.py).
_GIT_LOCK_TIMEOUT_SECONDS = float(os.getenv("AIFACTORY_GIT_LOCK_TIMEOUT", "120"))
_GIT_LOCK_FILENAME = "aifactory-worktree.lock"
# Bound the pre-worktree `git fetch origin <base>` (#1106) so an unreachable
# remote fails the build in a couple of minutes instead of hanging it.
_FETCH_TIMEOUT_SECONDS = 180.0
# `git check-ignore` is a purely local pattern match; if it has not answered in
# 30s something is wrong and the gitignore step should give up, not wedge.
_GIT_CHECK_IGNORE_TIMEOUT_SECONDS = 30.0
# git's verdict for "this branch is not on the remote", as opposed to an
# unreachable or unauthorised remote. Only this one is safe to proceed past
# (#1106): a branch the remote has never heard of cannot be behind it.
_REMOTE_REF_MISSING = "couldn't find remote ref"

# Ambient git env vars that pin git to ONE specific repo/index/work tree.
#
# This manager drives git across SEVERAL repos and worktrees (base repo +
# every linked worktree), so any inherited value here silently aims git at the
# wrong index. Git exports these into hook environments — notably
# `GIT_INDEX_FILE=.git/index` (relative!) during a pre-commit hook — so any
# AIFactory code that runs under a git hook inherits them. A relative
# GIT_INDEX_FILE re-resolves against each git child's own cwd; inside a linked
# worktree `.git` is a FILE, not a directory, so `git worktree add` dies with
# "fatal: .git/index: index file open failed: Not a directory" (#819).
_AMBIENT_GIT_VARS = frozenset(
    {
        "GIT_INDEX_FILE",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    }
)


def _git_env() -> dict[str, str]:
    """The current environment, minus the git vars that would pin git elsewhere.

    ``GIT_TERMINAL_PROMPT=0`` is forced on: this manager runs headless (control
    plane pod, build Job), so a remote that wants credentials must FAIL rather
    than block forever on a prompt nobody can answer (#1106 added a network
    fetch to the worktree path; a hang there would wedge the build).
    """
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_GIT_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# Artifacts a task worktree must never commit, each paired with a probe path
# used to ask git whether that rule is already in force here.
#
# Committing them makes concurrent parallel coders collide on merge-back
# (binary .coverage conflicts, duplicate .pyc) — found live in the 001
# benchmark. ``.aifactory/`` carries the factory's own runtime state
# (``status.json`` since #1106); a worktree inherits the base branch's tracked
# .gitignore, which is NOT where ``init.ensure_gitignore_entry`` writes, so the
# rule has to be re-stated here or the coder can stage factory internals into
# the PR the Approve control opens.
_ARTIFACT_RULES: tuple[tuple[str, str], ...] = (
    ("__pycache__/", "__pycache__/m.pyc"),
    ("*.py[cod]", "m.pyc"),
    (".coverage", ".coverage"),
    (".coverage.*", ".coverage.1"),
    ("htmlcov/", "htmlcov/index.html"),
    (".pytest_cache/", ".pytest_cache/CACHEDIR.TAG"),
    (".mypy_cache/", ".mypy_cache/m"),
    (".ruff_cache/", ".ruff_cache/m"),
    (".tox/", ".tox/m"),
    (".nox/", ".nox/m"),
    ("*.egg-info/", "m.egg-info/PKG-INFO"),
    (".eggs/", ".eggs/m"),
    ("dist/", "dist/m"),
    ("build/", "build/m"),
    ("node_modules/", "node_modules/m"),
    (".DS_Store", ".DS_Store"),
    (".aifactory/", ".aifactory/status.json"),
)
_GITIGNORE_HEADER = (
    "# Build / test artifacts (auto-added by AIFactory so parallel waves merge cleanly)"
)


def _unignored_artifact_rules(worktree_path: Path) -> list[str]:
    """The artifact patterns git does NOT already ignore in this worktree.

    ``git check-ignore`` is the authority, and asking it is the whole point of
    the check: it accounts for the worktree's own ``.gitignore``, nested ones,
    ``.git/info/exclude`` and the user's global excludes, and for rules spelled
    differently than ours (a project's ``*.pyc`` covers our ``*.py[cod]``). So
    "already done" means "these paths are ignored", not "this file happens to
    mention a string" — the #1172 defect, where any repo with a ``.coverage``
    line, i.e. most Python repos, silently received nothing.

    If git cannot answer (path is not a repo, git missing, timeout) fall back to
    "nothing is ignored" and let the caller write the full block, best-effort.
    """
    if not _ARTIFACT_RULES:  # pragma: no cover - defensive
        return []
    try:
        proc = subprocess.run(
            # S607: literal "git", no shell, fixed argv — same shape as the
            # sibling calls in this module. check=False: rc 1 ("nothing here is
            # ignored") is a normal answer, not a failure.
            ["git", "check-ignore", "--no-index", "--stdin"],  # noqa: S607
            cwd=str(worktree_path),
            input="\n".join(probe for _, probe in _ARTIFACT_RULES),
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
            timeout=_GIT_CHECK_IGNORE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("check-ignore unavailable in %s: %s", worktree_path, exc)
        return [pattern for pattern, _ in _ARTIFACT_RULES]
    # 0 = some path is ignored, 1 = none are; anything else (128: not a repo) is
    # an answer we did not get, so assume nothing is covered.
    if proc.returncode not in (0, 1):
        logger.debug(
            "check-ignore failed in %s: %s", worktree_path, proc.stderr.strip()[:200]
        )
        return [pattern for pattern, _ in _ARTIFACT_RULES]
    ignored = set(proc.stdout.splitlines())
    return [pattern for pattern, probe in _ARTIFACT_RULES if probe not in ignored]


def _ensure_artifact_gitignore(worktree_path: Path) -> None:
    """Make sure a worktree ignores build/test artifacts. Best-effort/no-raise.

    Appends ONLY the rules git does not already enforce here, as one clearly
    marked block, leaving whatever the managed project already had byte-for-byte
    intact — this is not a repo the factory owns. Idempotent by construction:
    after a run git ignores every pattern, so the next run finds nothing missing
    and writes nothing.
    """
    missing = _unignored_artifact_rules(worktree_path)
    if not missing:
        return
    block = _GITIGNORE_HEADER + "\n" + "".join(f"{pattern}\n" for pattern in missing)
    try:
        gi = Path(worktree_path) / ".gitignore"
        existing = (
            gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
        )
        if existing:
            if not existing.endswith("\n"):
                existing += "\n"
            existing += "\n"
        gi.write_text(existing + block, encoding="utf-8")
    except OSError as exc:
        logger.debug("could not ensure .gitignore in %s: %s", worktree_path, exc)


class WorktreeError(Exception):
    """Error during worktree operations."""

    pass


class _BaseRepoGitLock:
    """Exclusive cross-process flock on the shared base repo's .git.

    All concurrent builds that share a base repo agree on one sentinel file
    (``<git-common-dir>/aifactory-worktree.lock``) and take an exclusive
    ``fcntl.flock`` on it, so the actual git mutations queue instead of racing
    the shared index/refs. Bounded by ``timeout``; the sentinel is never
    unlinked (deleting an flock target races concurrent holders).
    """

    def __init__(
        self, lock_path: Path | str, timeout: float = _GIT_LOCK_TIMEOUT_SECONDS
    ):
        self._lock_path = Path(lock_path)
        self._timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "_BaseRepoGitLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        if fcntl is None:  # pragma: no cover - non-POSIX: degrade to no-op lock
            return self
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() - start >= self._timeout:
                    os.close(self._fd)
                    self._fd = None
                    raise WorktreeError(
                        f"Timed out after {self._timeout:.0f}s waiting for the "
                        f"base-repo git lock ({self._lock_path}). Another build "
                        f"is holding it, or a previous build wedged."
                    ) from None
                time.sleep(0.02)

    def __exit__(self, *_exc: object) -> Literal[False]:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


@dataclass
class WorktreeInfo:
    """Information about a spec's worktree."""

    path: Path
    branch: str
    spec_name: str
    base_branch: str
    is_active: bool = True
    commit_count: int = 0
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0


class WorktreeManager:
    """
    Manages per-spec Git worktrees.

    Each spec gets its own worktree in .aifactory/worktrees/tasks/{spec-name}/ with
    a corresponding branch aifactory/{spec-name}.
    """

    def __init__(self, project_dir: Path, base_branch: str | None = None):
        self.project_dir = project_dir
        # Self-heal a stray core.bare=true on the primary checkout BEFORE any
        # other git command runs (base-branch detection, worktree ops, ...).
        # See _ensure_not_bare for why this is needed.
        self._ensure_not_bare()
        self.base_branch = self._resolve_base_branch(base_branch)
        self.worktrees_dir = project_dir / ".aifactory" / "worktrees" / "tasks"
        self._merge_lock = asyncio.Lock()
        self._git_lock_path = self._resolve_git_lock_path()

    def _resolve_base_branch(self, requested: str | None) -> str:
        """Resolve the base branch a new worktree is cut from.

        A caller-supplied base (e.g. the ``/start`` payload's ``baseBranch``) is
        honoured ONLY when it resolves to a real commit in this repo. Otherwise
        ``git worktree add -b aifactory/<spec> <path> <base>`` fails with a
        WorktreeError, the build falls back to running on the primary checkout's
        branch (often ``main``), and the dedicated ``aifactory/<spec>`` branch is
        never created — so the AIFactory→TFactory handoff has no build branch to
        push and verify runs hollow. A missing/invalid base degrades to detection
        (DEFAULT_BRANCH → main/master → current) instead of blowing up the build.
        """
        if requested:
            result = self._run_git(["rev-parse", "--verify", "--quiet", requested])
            if result.returncode == 0:
                return requested  # exists → honour it
            # returncode 1 = valid repo, ref genuinely missing → fall back so the
            # worktree is cut from a real base. Any other code (e.g. 128 = not a
            # git repo / git unavailable) means we CAN'T validate — keep the
            # requested base verbatim (prior behaviour) rather than shelling out
            # to detection, which would raise outside a real repo.
            if result.returncode == 1:
                logger.warning(
                    "requested base_branch %r does not resolve in this repo; "
                    "falling back to detected base branch",
                    requested,
                )
                return self._detect_base_branch()
            return requested
        return self._detect_base_branch()

    def _resolve_start_point(self) -> str:
        """The revision a new task worktree is cut from, refreshed from origin (#1106).

        The checkout a build runs in is cloned once and reused, so its LOCAL
        base branch drifts behind ``origin/<base>`` by however many merges have
        landed since. Cutting the task branch from that stale ref produces a PR
        based on an old commit, which conflicts with everything merged in the
        meantime — the Factory#245 HITL demo hit exactly this
        (``mergeable_state: dirty``).

        So fetch ``origin`` and cut from the FETCHED tip. Deliberately does NOT
        move the local base ref (the #960 helper's ``reset --hard`` is safe only
        on the disposable kubejob clone; the subprocess backend shares one
        checkout with concurrent builds and a merge-back that has it checked
        out). Both backends route their build worktree through
        ``create_worktree``, so fixing it here covers subprocess and kubejob.

        Failure is LOUD — a fetch that fails RAISES rather than falling back to
        the local ref. Falling back is the whole bug: it produces a green-looking
        build whose PR cannot merge, discovered only by a human clicking Approve.
        A failed build says so immediately and costs one retry. The only case
        that proceeds is a repo with no ``origin`` at all, which has nothing to
        be stale against (offline dev, unit fixtures).

        A base branch that does not exist on the remote AT ALL is the same
        category as having no remote: there is nothing it could be behind, so it
        proceeds. This is the documented ``_detect_base_branch`` fallback (a repo
        with no main/master, cut from the current branch), which would otherwise
        hard-fail every build on a remote ref that was never supposed to exist.

        Note there is deliberately NO "the local base already matches
        ``origin/<base>``, so it must be fine" escape hatch. A remote-tracking
        ref is the memory of some earlier fetch, not evidence of currency — in a
        checkout that never fetched it matches the stale local branch exactly,
        which is the reported failure. Unreachable, unauthorised and unresolvable
        remotes all still raise; only a branch the remote has never heard of
        passes, and only on git's own "couldn't find remote ref" verdict.
        """
        remotes = self._run_git(["remote"])
        if remotes.returncode != 0 or "origin" not in remotes.stdout.split():
            logger.info(
                "no 'origin' remote in %s; cutting the worktree from the local "
                "base branch %r (#1106)",
                self.project_dir,
                self.base_branch,
            )
            return self.base_branch

        self._setup_git_credentials()
        fetch = self._run_git(
            ["fetch", "origin", self.base_branch], timeout=_FETCH_TIMEOUT_SECONDS
        )
        if fetch.returncode != 0:
            stderr = fetch.stderr.strip()
            if _REMOTE_REF_MISSING in stderr.lower():
                logger.warning(
                    "base branch %r does not exist on origin; it is local-only, so "
                    "there is no remote tip to be behind — cutting the worktree "
                    "from the local ref (#1106)",
                    self.base_branch,
                )
                return self.base_branch
            raise WorktreeError(
                f"Could not fetch base branch '{self.base_branch}' from origin in "
                f"{self.project_dir}:\n"
                f"  {fetch.stderr.strip() or 'no stderr'}\n"
                f"\n"
                f"Refusing to cut a build branch from an unverified base. A stale "
                f"base is what produces a PR that cannot be merged (#1106), which "
                f"breaks the Approve control silently. Fix the remote or its "
                f"credentials and retry the build."
            )

        logger.info(
            "refreshed base %r from origin before cutting the worktree (#1106)",
            self.base_branch,
        )
        # FETCH_HEAD was just written by the fetch above to the fetched tip. More
        # reliable than origin/<base>, which a remote configured without a
        # fetch refspec leaves unwritten.
        return "FETCH_HEAD"

    def _setup_git_credentials(self) -> None:
        """Wire ``gh`` as git's credential helper, best-effort (#540 pattern).

        The deployed pod and the build Job hold a gh token but no git credential
        config, so a raw HTTPS ``git fetch``/``git push`` fails with "could not
        read Username for https://github.com". Same call the PR push path
        already makes; done here so the #1106 fetch above fails only for a
        genuinely broken remote, never for a missing credential helper.
        """
        try:
            subprocess.run(
                ["gh", "auth", "setup-git"],  # noqa: S607 - fixed argv, no shell
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=15,
                env=_git_env(),
                check=False,  # advisory: the fetch below reports the real outcome
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # gh absent (local dev) or slow — the fetch below reports the real
            # outcome, so nothing to escalate here.
            logger.debug("gh auth setup-git skipped: %s", exc)

    def _resolve_git_lock_path(self) -> Path:
        """Path to the per-base-repo git mutation sentinel.

        Lives in the git COMMON dir so every linked worktree and every
        concurrent build that shares this base repo lands on the same sentinel
        (a worktree's ``.git`` file points back here). Falls back to
        ``project_dir/.git`` if resolution fails.
        """
        try:
            # Route through _run_git (not a raw subprocess) so we don't add a
            # new subprocess call site to the strict-ruff baseline.
            result = self._run_git(["rev-parse", "--git-common-dir"])
            if result.returncode == 0 and result.stdout.strip():
                git_dir = Path(result.stdout.strip())
                if not git_dir.is_absolute():
                    git_dir = (self.project_dir / git_dir).resolve()
                return git_dir / _GIT_LOCK_FILENAME
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("git-common-dir resolution failed: %s", exc)
        return self.project_dir / ".git" / _GIT_LOCK_FILENAME

    @contextmanager
    def _base_repo_git_lock(self) -> Iterator[None]:
        """Serialize cross-process git mutations on the shared base repo.

        Wrap any operation that touches the shared ``.git`` (index, refs,
        ``.git/worktrees``) so concurrent builds queue briefly instead of
        racing git's index.lock. Bounded; raises WorktreeError on timeout.
        """
        with _BaseRepoGitLock(self._git_lock_path):
            yield

    def _ensure_not_bare(self) -> None:
        """Guarantee the primary checkout is not marked as a bare repository.

        A normal checkout that hosts linked worktrees must have
        ``core.bare=false``. Certain external tools and git operations can leave
        ``core.bare=true`` on the primary repo, after which every plain git
        command fails with ``fatal: this operation must be run in a work tree``
        — breaking worktree creation, status checks, and merges. Because a
        WorktreeManager is constructed on every build/worktree action, healing
        it here keeps the repo healthy for AIFactory *and* any other git user
        (CI scripts, the host shell). Best-effort: never raise.
        """
        try:
            result = subprocess.run(
                ["git", "config", "--local", "core.bare"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_git_env(),
            )
            if result.stdout.strip() == "true":
                subprocess.run(
                    ["git", "config", "--local", "core.bare", "false"],
                    cwd=self.project_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=_git_env(),
                )
                logger.warning(
                    "Reset core.bare=true -> false on primary checkout %s "
                    "(a bare-marked checkout breaks all worktree git ops)",
                    self.project_dir,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("core.bare self-heal skipped: %s", exc)

    def _detect_base_branch(self) -> str:
        """
        Detect the base branch for worktree creation.

        Priority order:
        1. DEFAULT_BRANCH environment variable
        2. Auto-detect main/master (if they exist)
        3. Fall back to current branch (with warning)

        Returns:
            The detected base branch name
        """
        # 1. Check for DEFAULT_BRANCH env var
        env_branch = os.getenv("DEFAULT_BRANCH")
        if env_branch:
            # Verify the branch exists
            result = subprocess.run(
                ["git", "rev-parse", "--verify", env_branch],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_git_env(),
            )
            if result.returncode == 0:
                return env_branch
            else:
                print(
                    f"Warning: DEFAULT_BRANCH '{env_branch}' not found, auto-detecting..."
                )
                logger.warning(
                    f"DEFAULT_BRANCH '{env_branch}' not found, auto-detecting base branch"
                )

        # 2. Auto-detect main/master
        for branch in ["main", "master"]:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_git_env(),
            )
            if result.returncode == 0:
                return branch

        # 3. Fall back to current branch with warning
        current = self._get_current_branch()
        print("Warning: Could not find 'main' or 'master' branch.")
        print(f"Warning: Using current branch '{current}' as base for worktree.")
        print("Tip: Set DEFAULT_BRANCH=your-branch in .env to avoid this.")
        logger.warning(
            f"Could not find 'main' or 'master' branch. Using current branch '{current}' as base for worktree.",
            extra={
                "project_dir": str(self.project_dir),
                "current_branch": current,
            },
        )
        return current

    def _get_current_branch(self) -> str:
        """Get the current git branch."""
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
        )
        if result.returncode != 0:
            raise WorktreeError(f"Failed to get current branch: {result.stderr}")
        return result.stdout.strip()

    def _run_git(
        self, args: list[str], cwd: Path | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        """Run a git command and return the result.

        ``-c core.bare=false`` makes every worktree operation immune to a stray
        bare flag on the repo config, even if something re-set it between
        construction and this call (see _ensure_not_bare).

        ``env=_git_env()`` drops inherited GIT_DIR/GIT_INDEX_FILE/... so the
        command targets the repo at ``cwd`` and not whatever repo the ambient
        environment points at (see _AMBIENT_GIT_VARS).
        """
        return subprocess.run(
            ["git", "-c", "core.bare=false"] + args,
            cwd=cwd or self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
            timeout=timeout,
        )

    def _unstage_gitignored_files(self) -> None:
        """
        Unstage any staged files that are gitignored in the current branch,
        plus any files in the .aifactory directory which should never be merged.

        This is needed after a --no-commit merge because files that exist in the
        source branch (like spec files in .aifactory/specs/) get staged even if
        they're gitignored in the target branch.
        """
        # Get list of staged files
        result = self._run_git(["diff", "--cached", "--name-only"])
        if result.returncode != 0 or not result.stdout.strip():
            return

        staged_files = result.stdout.strip().split("\n")

        # Files to unstage: gitignored files + .aifactory directory files
        files_to_unstage = set()

        # 1. Check which staged files are gitignored
        # git check-ignore returns the files that ARE ignored
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=self.project_dir,
            input="\n".join(staged_files),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
        )

        if result.stdout.strip():
            for file in result.stdout.strip().split("\n"):
                if file.strip():
                    files_to_unstage.add(file.strip())

        # 2. Always unstage .aifactory directory files - these are project-specific
        # and should never be merged from the worktree branch
        magestic_ai_patterns = [".aifactory/", "aifactory/specs/"]
        # Root-level runtime files that agents may commit despite .gitignore
        magestic_ai_root_files = {".aifactory-security.json", ".aifactory-status"}
        for file in staged_files:
            file = file.strip()
            if not file:
                continue
            if file in magestic_ai_root_files:
                files_to_unstage.add(file)
                continue
            for pattern in magestic_ai_patterns:
                if file.startswith(pattern) or f"/{pattern}" in file:
                    files_to_unstage.add(file)
                    break

        if files_to_unstage:
            print(f"Unstaging {len(files_to_unstage)} aifactory/gitignored file(s)...")
            logger.info(
                f"Unstaging {len(files_to_unstage)} aifactory/gitignored files",
                extra={
                    "files": list(files_to_unstage)[:10],  # Log first 10 files
                    "total_count": len(files_to_unstage),
                },
            )
            # Unstage each file
            for file in files_to_unstage:
                self._run_git(["reset", "HEAD", "--", file])

    def setup(self) -> None:
        """Create worktrees directory if needed."""
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    # ==================== Per-Spec Worktree Methods ====================

    def get_worktree_path(self, spec_name: str) -> Path:
        """Get the worktree path for a spec."""
        return self.worktrees_dir / spec_name

    def get_branch_name(self, spec_name: str) -> str:
        """Get the branch name for a spec."""
        return f"aifactory/{spec_name}"

    def worktree_exists(self, spec_name: str) -> bool:
        """Check if a worktree exists for a spec."""
        return self.get_worktree_path(spec_name).exists()

    def get_worktree_info(self, spec_name: str) -> WorktreeInfo | None:
        """Get info about a spec's worktree."""
        worktree_path = self.get_worktree_path(spec_name)
        if not worktree_path.exists():
            return None

        # Verify this is a real git worktree (has .git FILE, not directory)
        # Git worktrees have a .git file that points to the main repo's .git/worktrees/
        # A regular directory inside the repo would not have this
        git_path = worktree_path / ".git"
        if not git_path.exists() or not git_path.is_file():
            # Directory exists but is not a valid worktree
            return None

        # Verify the branch exists in the worktree
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
        if result.returncode != 0:
            return None

        actual_branch = result.stdout.strip()

        # Get statistics
        stats = self._get_worktree_stats(spec_name)

        return WorktreeInfo(
            path=worktree_path,
            branch=actual_branch,
            spec_name=spec_name,
            base_branch=self.base_branch,
            is_active=True,
            **stats,
        )

    def _check_branch_namespace_conflict(self) -> str | None:
        """
        Check if a branch named 'aifactory' exists, which would block creating
        branches in the 'aifactory/*' namespace.

        Git stores branch refs as files under .git/refs/heads/, so a branch named
        'aifactory' creates a file that prevents creating the 'aifactory/'
        directory needed for 'aifactory/{spec-name}' branches.

        Returns:
            The conflicting branch name if found, None otherwise.
        """
        result = self._run_git(["rev-parse", "--verify", "aifactory"])
        if result.returncode == 0:
            return "aifactory"
        return None

    def _get_worktree_stats(self, spec_name: str) -> dict:
        """Get diff statistics for a worktree."""
        worktree_path = self.get_worktree_path(spec_name)

        stats = {
            "commit_count": 0,
            "files_changed": 0,
            "additions": 0,
            "deletions": 0,
        }

        if not worktree_path.exists():
            return stats

        # Compare against the REMOTE base, matching what the worktree was cut
        # from (#1106). The local base ref lags origin, and now that the branch
        # is cut from the fetched tip every commit the checkout had not seen
        # would otherwise be counted as this task's work — a task with two
        # commits reporting dozens in the cockpit.
        base = (
            f"origin/{self.base_branch}"
            if self._run_git(
                [
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{self.base_branch}",
                ]
            ).returncode
            == 0
            else self.base_branch
        )

        # Commit count
        result = self._run_git(
            ["rev-list", "--count", f"{base}..HEAD"], cwd=worktree_path
        )
        if result.returncode == 0:
            stats["commit_count"] = int(result.stdout.strip() or "0")

        # Diff stats
        result = self._run_git(
            ["diff", "--shortstat", f"{base}...HEAD"], cwd=worktree_path
        )
        if result.returncode == 0 and result.stdout.strip():
            # Parse: "3 files changed, 50 insertions(+), 10 deletions(-)"
            match = re.search(r"(\d+) files? changed", result.stdout)
            if match:
                stats["files_changed"] = int(match.group(1))
            match = re.search(r"(\d+) insertions?", result.stdout)
            if match:
                stats["additions"] = int(match.group(1))
            match = re.search(r"(\d+) deletions?", result.stdout)
            if match:
                stats["deletions"] = int(match.group(1))

        return stats

    def create_worktree(self, spec_name: str) -> WorktreeInfo:
        """
        Create a worktree for a spec.

        Args:
            spec_name: The spec folder name (e.g., "002-implement-memory")

        Returns:
            WorktreeInfo for the created worktree

        Raises:
            WorktreeError: If a branch namespace conflict exists or worktree creation fails
        """
        worktree_path = self.get_worktree_path(spec_name)
        branch_name = self.get_branch_name(spec_name)

        # Check for branch namespace conflict (e.g., 'aifactory' blocking 'aifactory/*')
        conflicting_branch = self._check_branch_namespace_conflict()
        if conflicting_branch:
            raise WorktreeError(
                f"Branch '{conflicting_branch}' exists and blocks creating '{branch_name}'.\n"
                f"\n"
                f"Git branch names work like file paths - a branch named 'aifactory' prevents\n"
                f"creating branches under 'aifactory/' (like 'aifactory/{spec_name}').\n"
                f"\n"
                f"Fix: Rename the conflicting branch:\n"
                f"  git branch -m {conflicting_branch} {conflicting_branch}-backup"
            )

        # Cut the branch from the CURRENT remote tip, not the checkout's stale
        # local base ref (#1106). Raises WorktreeError when the base cannot be
        # verified as current — see _resolve_start_point.
        start_point = self._resolve_start_point()

        # Serialize the shared-.git mutations below across CONCURRENT BUILDS:
        # `worktree remove`, `branch -D`, and `worktree add` all take git's
        # index.lock and rewrite .git/worktrees + refs. Two builds racing here
        # (now possible under admission control #672) hit index-lock contention
        # or corrupt refs. The lock is per-base-repo and bounded.
        with self._base_repo_git_lock():
            # Remove existing if present (from crashed previous run or pre-created directory)
            if worktree_path.exists():
                # Check if it's a real worktree (has .git file)
                git_path = worktree_path / ".git"
                if git_path.exists() and git_path.is_file():
                    # Real worktree - use git worktree remove
                    self._run_git(["worktree", "remove", "--force", str(worktree_path)])
                else:
                    # Not a real worktree (e.g., pre-created by agent_service)
                    # Just delete the directory
                    shutil.rmtree(worktree_path, ignore_errors=True)

            # Delete branch if it exists (from previous attempt)
            self._run_git(["branch", "-D", branch_name])

            # Create worktree with new branch from base
            result = self._run_git(
                [
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    start_point,
                ]
            )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to create worktree for {spec_name}: {result.stderr}"
            )

        print(f"Created worktree: {worktree_path.name} on branch {branch_name}")
        logger.info(
            f"Created worktree for spec '{spec_name}'",
            extra={
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "base_branch": self.base_branch,
            },
        )

        # Ensure build/test artifacts are gitignored so concurrent coders never
        # commit them. Without this, each parallel coder commits .coverage /
        # __pycache__ etc., which then COLLIDE on sequential merge-back (binary
        # conflict → wave aborts to serial). Found live in the 001 benchmark.
        _ensure_artifact_gitignore(worktree_path)

        return WorktreeInfo(
            path=worktree_path,
            branch=branch_name,
            spec_name=spec_name,
            base_branch=self.base_branch,
            is_active=True,
        )

    def get_or_create_worktree(self, spec_name: str) -> WorktreeInfo:
        """
        Get existing worktree or create a new one for a spec.

        Args:
            spec_name: The spec folder name

        Returns:
            WorktreeInfo for the worktree
        """
        existing = self.get_worktree_info(spec_name)
        if existing:
            print(f"Using existing worktree: {existing.path}")
            logger.info(
                f"Using existing worktree for spec '{spec_name}'",
                extra={
                    "worktree_path": str(existing.path),
                    "branch": existing.branch,
                },
            )
            return existing

        return self.create_worktree(spec_name)

    def remove_worktree(self, spec_name: str, delete_branch: bool = False) -> None:
        """
        Remove a spec's worktree.

        Args:
            spec_name: The spec folder name
            delete_branch: Whether to also delete the branch
        """
        worktree_path = self.get_worktree_path(spec_name)
        branch_name = self.get_branch_name(spec_name)

        # Same shared-.git mutations as create_worktree (remove/branch -D/prune);
        # serialize across concurrent builds on the per-base-repo lock.
        with self._base_repo_git_lock():
            if worktree_path.exists():
                result = self._run_git(
                    ["worktree", "remove", "--force", str(worktree_path)]
                )
                if result.returncode == 0:
                    print(f"Removed worktree: {worktree_path.name}")
                    logger.info(
                        f"Removed worktree for spec '{spec_name}'",
                        extra={
                            "worktree_path": str(worktree_path),
                        },
                    )
                else:
                    print(f"Warning: Could not remove worktree: {result.stderr}")
                    logger.warning(
                        "Could not remove worktree via git, falling back to rmtree",
                        extra={
                            "worktree_path": str(worktree_path),
                            "error": result.stderr,
                        },
                    )
                    shutil.rmtree(worktree_path, ignore_errors=True)

            if delete_branch:
                self._run_git(["branch", "-D", branch_name])
                print(f"Deleted branch: {branch_name}")
                logger.info(f"Deleted branch '{branch_name}'")

            self._run_git(["worktree", "prune"])

    def merge_worktree(
        self, spec_name: str, delete_after: bool = False, no_commit: bool = False
    ) -> bool:
        """
        Merge a spec's worktree branch back to base branch.

        Args:
            spec_name: The spec folder name
            delete_after: Whether to remove worktree and branch after merge
            no_commit: If True, merge changes but don't commit (stage only for review)

        Returns:
            True if merge succeeded
        """
        info = self.get_worktree_info(spec_name)
        if not info:
            print(f"No worktree found for spec: {spec_name}")
            logger.warning(
                f"Merge attempted but no worktree found for spec '{spec_name}'"
            )
            return False

        # Security pre-merge gate (#415, default-off via AIFACTORY_SELF_HEAL):
        # scan the branch diff for secrets/injection and refuse to merge a
        # high-severity finding. No-op unless the flag is enabled.
        try:
            from agents.self_heal_integration import security_pre_merge_gate_sync

            _diff = self._run_git(["diff", f"{self.base_branch}...{info.branch}"])
            _decision = security_pre_merge_gate_sync(_diff.stdout or "")
            if _decision is not None and _decision.blocked:
                print(
                    f"Security gate BLOCKED merge of {info.branch}: {_decision.summary}"
                )
                logger.error(
                    f"Security pre-merge gate blocked merge for spec '{spec_name}'",
                    extra={"branch": info.branch, "summary": _decision.summary},
                )
                return False
        except Exception:
            pass  # gate must never crash a merge that was otherwise fine

        if no_commit:
            print(
                f"Merging {info.branch} into {self.base_branch} (staged, not committed)..."
            )
            logger.info(
                f"Starting staged merge (no-commit) for spec '{spec_name}'",
                extra={
                    "branch": info.branch,
                    "base_branch": self.base_branch,
                },
            )
        else:
            print(f"Merging {info.branch} into {self.base_branch}...")
            logger.info(
                f"Starting merge for spec '{spec_name}'",
                extra={
                    "branch": info.branch,
                    "base_branch": self.base_branch,
                },
            )

        # The body below mutates the SHARED base repo (checkout/stash/merge on
        # its working tree, index, HEAD and refs). Under admission control
        # (#672) another concurrent build could be doing `git worktree add` or
        # its own merge against the same .git at the same instant → index.lock
        # contention / ref corruption. Serialize on the per-base-repo lock.
        # delete_after (worktree/branch removal) re-enters the lock via
        # remove_worktree, so it stays OUTSIDE this block (flock is not
        # reentrant across separate fds in one process).
        with self._base_repo_git_lock():
            merged = self._do_merge_locked(spec_name, info, no_commit)

        if not merged:
            return False

        if delete_after:
            self.remove_worktree(spec_name, delete_branch=True)

        return True

    def _do_merge_locked(
        self, spec_name: str, info: WorktreeInfo, no_commit: bool
    ) -> bool:
        """Perform the base-repo mutating merge. Caller MUST hold the git lock."""
        # Clean up internal auto-generated files that can block merge/checkout.
        # These are untracked files created by agents that would collide with
        # the same untracked files coming from the worktree branch.
        _INTERNAL_MERGE_BLOCKERS = [
            ".aifactory-security.json",
            ".aifactory-status",
        ]
        for fname in _INTERNAL_MERGE_BLOCKERS:
            blocker = self.project_dir / fname
            if blocker.exists():
                try:
                    blocker.unlink()
                    logger.info(f"Removed merge-blocking file: {fname}")
                except OSError:
                    pass

        # The smart-merge step (or an agent) can leave the base working tree
        # dirty on TRACKED files — notably it rewrites .gitignore — which makes
        # the checkout/merge below abort with "Your local changes to <file>
        # would be overwritten by merge" → a bogus "Merge conflict" (#485). The
        # worktree branch is the source of truth, so stash any uncommitted
        # changes (incl. untracked) first; drop the stash after a successful
        # merge (the merge brings the branch's version of those files), or
        # restore it if the merge fails.
        stashed = False
        status = self._run_git(["status", "--porcelain"])
        if status.returncode == 0 and status.stdout.strip():
            stash = self._run_git(
                [
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    f"aifactory pre-merge {spec_name}",
                ]
            )
            stashed = stash.returncode == 0 and "No local changes" not in (
                stash.stdout or ""
            )
            if stashed:
                logger.info(f"Stashed pre-merge working-tree changes for '{spec_name}'")

        # Switch to base branch in main project
        result = self._run_git(["checkout", self.base_branch])
        if result.returncode != 0:
            print(f"Error: Could not checkout base branch: {result.stderr}")
            logger.error(
                f"Could not checkout base branch '{self.base_branch}' for merge",
                extra={
                    "spec_name": spec_name,
                    "error": result.stderr,
                },
            )
            if stashed:
                self._run_git(["stash", "pop"])
            return False

        # Merge the spec branch
        merge_args = ["merge", "--no-ff", info.branch]
        if no_commit:
            # --no-commit stages the merge but doesn't create the commit
            merge_args.append("--no-commit")
        else:
            merge_args.extend(["-m", f"aifactory: Merge {info.branch}"])

        result = self._run_git(merge_args)

        if result.returncode != 0:
            print("Merge conflict! Aborting merge...")
            logger.error(
                f"Merge conflict detected for spec '{spec_name}', aborting",
                extra={
                    "branch": info.branch,
                    "base_branch": self.base_branch,
                    "error": result.stderr,
                },
            )
            self._run_git(["merge", "--abort"])
            if stashed:
                # Restore the caller's working-tree changes after the aborted merge.
                self._run_git(["stash", "pop"])
            return False

        # Merge succeeded: the worktree branch's content (incl. .gitignore) is
        # now in the tree, so the stashed pre-merge edits are superseded — drop
        # them rather than pop (a pop would re-conflict on the same files).
        if stashed:
            self._run_git(["stash", "drop"])

        if no_commit:
            # Unstage any files that are gitignored in the main branch
            # These get staged during merge because they exist in the worktree branch
            self._unstage_gitignored_files()
            print(
                f"Changes from {info.branch} are now staged in your working directory."
            )
            print("Review the changes, then commit when ready:")
            print("  git commit -m 'your commit message'")
            logger.info(
                f"Staged merge completed for spec '{spec_name}' (no-commit mode)",
                extra={
                    "branch": info.branch,
                    "base_branch": self.base_branch,
                },
            )
        else:
            print(f"Successfully merged {info.branch}")
            logger.info(
                f"Successfully merged spec '{spec_name}'",
                extra={
                    "branch": info.branch,
                    "base_branch": self.base_branch,
                },
            )

        return True

    def commit_in_worktree(self, spec_name: str, message: str) -> bool:
        """Commit all changes in a spec's worktree."""
        worktree_path = self.get_worktree_path(spec_name)
        if not worktree_path.exists():
            return False

        add = self._run_git(["add", ".", ":!.aifactory"], cwd=worktree_path)
        result = self._run_git(["commit", "-m", message], cwd=worktree_path)

        if result.returncode == 0:
            return True
        # An empty commit is a no-op, not a failure. git writes these to STDOUT
        # (not stderr) with a non-zero rc, and phrases it several ways — match all
        # of them across BOTH streams (#994: the old check only looked for
        # "nothing to commit" and only printed stderr, so a "no changes added to
        # commit" on stdout surfaced as a useless empty "Commit failed:").
        combined = (result.stdout or "") + (result.stderr or "")
        if any(
            m in combined
            for m in (
                "nothing to commit",
                "no changes added to commit",
                "working tree clean",
            )
        ):
            return True

        # Real failure: surface EVERYTHING (rc + stdout + stderr, plus the git-add
        # result), because git commit errors frequently land on stdout and an empty
        # stderr previously left the build churning with no diagnosable cause (#994).
        detail = (
            f"rc={result.returncode} "
            f"stderr={(result.stderr or '').strip()!r} "
            f"stdout={(result.stdout or '').strip()!r}"
        )
        if add.returncode != 0:
            detail += (
                f" | git-add rc={add.returncode} stderr={(add.stderr or '').strip()!r}"
            )
        print(f"Commit failed: {detail}")
        logger.error(
            f"Commit failed in worktree for spec '{spec_name}'",
            extra={
                "worktree_path": str(worktree_path),
                "error": detail,
                # NB: key must NOT be "message" — that's a reserved LogRecord
                # attribute and logging raises "Attempt to overwrite 'message'
                # in LogRecord", which previously propagated out of a parallel
                # subtask and aborted the whole wave to serial (#376 regression).
                "commit_message": message,
            },
        )
        return False

    # ==================== Listing & Discovery ====================

    def list_all_worktrees(self) -> list[WorktreeInfo]:
        """List all spec worktrees."""
        worktrees = []

        if self.worktrees_dir.exists():
            for item in self.worktrees_dir.iterdir():
                if item.is_dir():
                    info = self.get_worktree_info(item.name)
                    if info:
                        worktrees.append(info)

        return worktrees

    def list_all_spec_branches(self) -> list[str]:
        """List all aifactory branches (even if worktree removed)."""
        result = self._run_git(["branch", "--list", "aifactory/*"])
        if result.returncode != 0:
            return []

        branches = []
        for line in result.stdout.strip().split("\n"):
            branch = line.strip().lstrip("* ")
            if branch:
                branches.append(branch)

        return branches

    def discover_pushed_ref(self, spec_name: str) -> str | None:
        """The remote-tracking ref holding *spec_name*'s work, or None (#1089).

        Under ``AIFACTORY_BUILD_BACKEND=kubejob`` the build runs in a separate
        pod and the work escapes only by ``git push``; this manager's worktree
        is deliberately left on the BASE branch (see
        ``build_backend._populate_self_contained_worktree``). Anything asking
        "what did this task change" must therefore read the pushed ref in the
        project repo, not the worktree's HEAD.

        This class owns the branch convention (``get_branch_name``), so the
        lookup is exact rather than a pattern match. The canonical control-plane
        resolver is ``server/services/task_branch.resolve_work_ref``; if the two
        ever disagree, that one is right.
        """
        ref = f"origin/{self.get_branch_name(spec_name)}"
        result = self._run_git(["rev-parse", "--verify", "--quiet", ref])
        return ref if result.returncode == 0 else None

    def get_changed_files(self, spec_name: str) -> list[tuple[str, str]]:
        """Get changed files for a spec, from the pushed ref when there is one."""
        worktree_path = self.get_worktree_path(spec_name)
        if not worktree_path.exists():
            return []

        # #1089: `{base}...HEAD` in the worktree is base-against-base whenever the
        # build pushed from somewhere else, and this feeds `--review`, `--merge`
        # and `--discard`, all of which then print "No changes were made." The
        # pushed ref read in the project repo is the same range against the real
        # work. With nothing pushed (subprocess builds, in-Job) the worktree HEAD
        # IS the task branch and the read below is unchanged.
        work_ref = self.discover_pushed_ref(spec_name)
        head = work_ref or "HEAD"
        git_cwd = self.project_dir if work_ref else worktree_path
        result = self._run_git(
            ["diff", "--name-status", f"{self.base_branch}...{head}"], cwd=git_cwd
        )

        files = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                files.append((parts[0], parts[1]))

        return files

    def get_change_summary(self, spec_name: str) -> dict:
        """Get a summary of changes in a worktree."""
        files = self.get_changed_files(spec_name)

        new_files = sum(1 for status, _ in files if status == "A")
        modified_files = sum(1 for status, _ in files if status == "M")
        deleted_files = sum(1 for status, _ in files if status == "D")

        return {
            "new_files": new_files,
            "modified_files": modified_files,
            "deleted_files": deleted_files,
        }

    def cleanup_all(self) -> None:
        """Remove all worktrees and their branches."""
        for worktree in self.list_all_worktrees():
            self.remove_worktree(worktree.spec_name, delete_branch=True)

    def cleanup_stale_worktrees(self) -> None:
        """Remove worktrees that aren't registered with git."""
        if not self.worktrees_dir.exists():
            return

        # Get list of registered worktrees
        result = self._run_git(["worktree", "list", "--porcelain"])
        registered_paths = set()
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                registered_paths.add(Path(line.split(" ", 1)[1]))

        # Remove unregistered directories
        for item in self.worktrees_dir.iterdir():
            if item.is_dir() and item not in registered_paths:
                print(f"Removing stale worktree directory: {item.name}")
                shutil.rmtree(item, ignore_errors=True)

        self._run_git(["worktree", "prune"])

    def get_test_commands(self, spec_name: str) -> list[str]:
        """Detect likely test/run commands for the project."""
        worktree_path = self.get_worktree_path(spec_name)
        commands = []

        if (worktree_path / "package.json").exists():
            commands.append("npm install && npm run dev")
            commands.append("npm test")

        if (worktree_path / "requirements.txt").exists():
            commands.append("pip install -r requirements.txt")

        if (worktree_path / "Cargo.toml").exists():
            commands.append("cargo run")
            commands.append("cargo test")

        if (worktree_path / "go.mod").exists():
            commands.append("go run .")
            commands.append("go test ./...")

        if not commands:
            commands.append("# Check the project's README for run instructions")

        return commands

    def has_uncommitted_changes(self, spec_name: str | None = None) -> bool:
        """Check if there are uncommitted changes."""
        cwd = None
        if spec_name:
            worktree_path = self.get_worktree_path(spec_name)
            if worktree_path.exists():
                cwd = worktree_path
        result = self._run_git(["status", "--porcelain"], cwd=cwd)
        return bool(result.stdout.strip())
