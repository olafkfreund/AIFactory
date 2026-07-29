"""
Modification Tracking Module
=============================

Handles recording and analyzing file modifications:
- Recording task modifications with semantic analysis
- Refreshing modifications from git worktrees
- Managing task completion status
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from ..semantic_analyzer import SemanticAnalyzer
from ..types import FileEvolution, TaskSnapshot, compute_content_hash
from .storage import EvolutionStorage

# Import debug utilities
try:
    from debug import debug, debug_warning
except ImportError:

    def debug(*args, **kwargs):
        pass

    def debug_warning(*args, **kwargs):
        pass


logger = logging.getLogger(__name__)
MODULE = "merge.file_evolution.modification_tracker"

# Files/patterns to ignore during conflict detection (internal auto-generated files)
IGNORED_FILES = {
    ".aifactory-security.json",
    ".aifactory-status",
}
IGNORED_PREFIXES = (
    ".aifactory/",
    "VERIFICATION_REPORT",
    "LANGUAGE_CHOICE",
)


class ModificationTracker:
    """
    Manages tracking of file modifications by tasks.

    Responsibilities:
    - Record modifications with semantic analysis
    - Refresh modifications from git worktrees
    - Mark tasks as completed
    """

    def __init__(
        self,
        storage: EvolutionStorage,
        semantic_analyzer: SemanticAnalyzer | None = None,
    ):
        """
        Initialize modification tracker.

        Args:
            storage: Storage manager for file operations
            semantic_analyzer: Optional pre-configured semantic analyzer
        """
        self.storage = storage
        self.analyzer = semantic_analyzer or SemanticAnalyzer()

    def record_modification(
        self,
        task_id: str,
        file_path: Path | str,
        old_content: str,
        new_content: str,
        evolutions: dict[str, FileEvolution],
        raw_diff: str | None = None,
    ) -> TaskSnapshot | None:
        """
        Record a file modification by a task.

        Args:
            task_id: The task that made the modification
            file_path: Path to the modified file
            old_content: File content before modification
            new_content: File content after modification
            evolutions: Current evolution data (will be updated)
            raw_diff: Optional unified diff for reference

        Returns:
            Updated TaskSnapshot, or None if file not being tracked
        """
        rel_path = self.storage.get_relative_path(file_path)

        # Get or create evolution
        if rel_path not in evolutions:
            logger.warning(f"File {rel_path} not being tracked")
            # Note: We could auto-create here, but for now return None
            return None

        evolution = evolutions.get(rel_path)
        if not evolution:
            return None

        # Get existing snapshot or create new one
        snapshot = evolution.get_task_snapshot(task_id)
        if not snapshot:
            snapshot = TaskSnapshot(
                task_id=task_id,
                task_intent="",
                started_at=datetime.now(),
                content_hash_before=compute_content_hash(old_content),
            )

        # Analyze semantic changes
        analysis = self.analyzer.analyze_diff(rel_path, old_content, new_content)
        semantic_changes = analysis.changes

        # Update snapshot
        snapshot.completed_at = datetime.now()
        snapshot.content_hash_after = compute_content_hash(new_content)
        snapshot.semantic_changes = semantic_changes
        snapshot.raw_diff = raw_diff

        # Update evolution
        evolution.add_task_snapshot(snapshot)

        logger.info(
            f"Recorded modification to {rel_path} by {task_id}: "
            f"{len(semantic_changes)} semantic changes"
        )
        return snapshot

    def refresh_from_git(  # noqa: PLR0913 - see below
        self,
        task_id: str,
        worktree_path: Path,
        evolutions: dict[str, FileEvolution],
        target_branch: str | None = None,
        work_ref: str | None = None,
        repo_path: Path | None = None,
    ) -> None:
        """
        Refresh task snapshots by analyzing git diff from worktree.

        This is useful when we didn't capture real-time modifications
        and need to retroactively analyze what a task changed.

        Args:
            task_id: The task identifier
            worktree_path: Path to the task's worktree
            evolutions: Current evolution data (will be updated)
            target_branch: Branch to compare against (default: detect from worktree)
            work_ref: Ref holding the task's work, read in *repo_path* instead of
                the worktree's HEAD. Control-plane callers pass what
                ``resolve_work_ref`` found; in-Job callers omit it (#1089).
            repo_path: Repository to run git in when *work_ref* is given.

        PLR0913 is suppressed rather than fixed: the two new parameters are
        optional and additive precisely so that every existing in-Job caller
        keeps working unchanged. Bundling them into a config object to satisfy
        the argument count would force a signature change on callers that have
        no interest in either.
        """
        # #1089: under the kubejob build backend the control plane's worktree is
        # a standalone clone left on the BASE branch -- the work escapes the
        # build Job by `git push`. Reading `{target}...HEAD` there diffs base
        # against base, so the semantic conflict detector was handed an EMPTY
        # change set and reported zero conflicts for every task.
        #
        # Both arguments are required together: a ref without a repo to read it
        # in is not resolvable, and silently falling back to the worktree is the
        # bug. In-Job callers pass neither and behave exactly as before, which
        # matters because that is the majority caller and its worktree HEAD
        # genuinely IS the task branch.
        by_ref = bool(work_ref and repo_path)
        git_cwd = Path(repo_path) if by_ref else worktree_path
        head = work_ref if by_ref else "HEAD"

        # Determine the target branch to compare against
        if not target_branch:
            # Detect in whichever repository the refs are actually readable.
            target_branch = self._detect_target_branch(git_cwd)

        debug(
            MODULE,
            f"refresh_from_git() for task {task_id}",
            task_id=task_id,
            worktree_path=str(worktree_path),
            target_branch=target_branch,
        )

        try:
            # Get list of files changed in the worktree vs target branch
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{target_branch}...{head}"],
                cwd=git_cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            changed_files = [
                f
                for f in result.stdout.strip().split("\n")
                if f
                and Path(f).name not in IGNORED_FILES
                and not any(f.startswith(p) for p in IGNORED_PREFIXES)
            ]

            debug(
                MODULE,
                f"Found {len(changed_files)} changed files",
                changed_files=changed_files[:10]
                if len(changed_files) > 10
                else changed_files,
            )

            for file_path in changed_files:
                # Get the diff for this file
                diff_result = subprocess.run(
                    ["git", "diff", f"{target_branch}...{head}", "--", file_path],
                    cwd=git_cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                )

                # Get content before (from target branch) and after (current)
                try:
                    show_result = subprocess.run(
                        ["git", "show", f"{target_branch}:{file_path}"],
                        cwd=git_cwd,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    old_content = show_result.stdout
                except subprocess.CalledProcessError:
                    # File is new
                    old_content = ""

                if by_ref:
                    # The work is on a REF, not on disk: the control plane's
                    # worktree does not contain these files at all. Reading the
                    # filesystem here would silently yield "" for every file and
                    # record each one as a deletion.
                    try:
                        # S603/S607: same shape as the sibling calls in this
                        # method -- literal "git", no shell, and head/file_path
                        # come from git's own output, not from a caller.
                        after = subprocess.run(  # noqa: S603
                            ["git", "show", f"{head}:{file_path}"],  # noqa: S607
                            cwd=git_cwd,
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        new_content = after.stdout
                    except subprocess.CalledProcessError:
                        # Genuinely deleted on the task branch.
                        new_content = ""
                else:
                    current_file = worktree_path / file_path
                    if current_file.exists():
                        try:
                            new_content = current_file.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            new_content = current_file.read_text(
                                encoding="utf-8", errors="replace"
                            )
                    else:
                        # File was deleted
                        new_content = ""

                # Record the modification
                self.record_modification(
                    task_id=task_id,
                    file_path=file_path,
                    old_content=old_content,
                    new_content=new_content,
                    evolutions=evolutions,
                    raw_diff=diff_result.stdout,
                )

            logger.info(
                f"Refreshed {len(changed_files)} files from worktree for task {task_id}"
            )

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to refresh from git: {e}")

    def mark_task_completed(
        self,
        task_id: str,
        evolutions: dict[str, FileEvolution],
    ) -> None:
        """
        Mark a task as completed (set completed_at on all snapshots).

        Args:
            task_id: The task identifier
            evolutions: Current evolution data (will be updated)
        """
        now = datetime.now()
        for evolution in evolutions.values():
            snapshot = evolution.get_task_snapshot(task_id)
            if snapshot and snapshot.completed_at is None:
                snapshot.completed_at = now

    def _detect_target_branch(self, repo_path: Path) -> str:
        """
        Detect the target branch to compare against, in *repo_path*.

        Finds the branch the work was based on by looking at the merge-base
        between HEAD and common branch names.

        Named ``repo_path`` rather than ``worktree_path``: ``refresh_from_git``
        hands this the PROJECT repository when reading by ref (#1089). A
        parameter called worktree_path that receives the project repo is a
        comment that lies, and it is what made the enforcement check flag this
        line even though the runtime behaviour was already correct.

        Args:
            repo_path: Repository to detect in -- the task worktree in-Job, the
                project repo on the control plane

        Returns:
            The detected target branch name, defaults to 'main' if detection fails
        """
        # Try to get the upstream tracking branch
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                upstream = result.stdout.strip()
                # Extract branch name from origin/branch format
                if "/" in upstream:
                    return upstream.split("/", 1)[1]
                return upstream
        except subprocess.CalledProcessError:
            pass

        # Try common branch names and find which one has a valid merge-base
        for branch in ["main", "master", "develop"]:
            try:
                result = subprocess.run(
                    ["git", "merge-base", branch, "HEAD"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return branch
            except subprocess.CalledProcessError:
                continue

        # Default to main
        debug_warning(
            MODULE,
            "Could not detect target branch, defaulting to 'main'",
            repo_path=str(repo_path),
        )
        return "main"
