"""
Worktree merge / conflict-resolution routes.

Extracted verbatim from ``routes/tasks.py`` (issue #649, epic #154) as a
behavior-preserving sub-router. The endpoints are mounted onto the tasks router
via ``include_router`` so the public paths and request/response shapes are
unchanged:

    GET  /{task_id}/worktree/merge-preview
    POST /{task_id}/worktree/resolve-conflicts
    POST /{task_id}/worktree/resolve-uncommitted
    POST /{task_id}/worktree/resolve-git-merge
    POST /{task_id}/worktree/abort-merge
    POST /{task_id}/worktree/merge
    GET  /{task_id}/worktree/status
    GET  /{task_id}/worktree/diff
    POST /{task_id}/worktree/discard

The handlers depend only on already-extractable collaborators -- ``get_data_dir``
from ``..paths``, ``require_task_access`` from ``routes/project_authz``, and
``get_projects_file`` from ``routes/projects`` (none of which import this module,
so lifting the cluster out cannot create a circular import). The
``conflict_service`` collaborator is imported lazily inside the handlers, exactly
as it was in ``tasks.py``.

The request models ``WorktreeMergeOptions`` / ``ConflictResolveOptions`` and the
handlers ``merge_worktree`` / ``get_worktree_diff`` historically lived in
``routes/tasks.py`` and are re-exported there for backward compatibility
(``mcp_stdio/router.py`` imports them from ``..routes.tasks``).
"""

import json
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.services.task_branch import resolve_task_branch
from server.services.task_status import write_status
from server.specpath import safe_spec_component

from ..paths import get_data_dir
from .project_authz import require_task_access
from .projects import get_projects_file

router = APIRouter()


def _approved(project_path: Path, spec_id: str, message: str, **extra: object) -> dict:
    """Record the approval and shape the success response.

    Shared by the PR-merge and local-merge paths so they cannot disagree about
    what "merged" writes -- which is exactly how one of them ends up not
    writing it at all (#1071).

    spec_id, NOT task_id: task_id still carries the "project_id:" prefix, so
    joining it addresses a directory that does not exist -- and write_control
    would helpfully create it, landing the status nowhere.
    """
    status_error = write_status(
        project_path / ".aifactory" / "specs" / spec_id,
        status="done",
        reason=f"approved: {message}",
        updated_by="approve-merge",
    )
    if status_error:
        # Reported, not swallowed. The merge really happened and cannot be
        # undone, so this is not a failure of the request -- but a caller told
        # "merged" while the board still says human_review deserves to know why.
        logger.warning(
            "merged %s but could not record status: %s",
            spec_id.replace("\n", " "),
            status_error.replace("\n", " "),
        )
    return {
        "success": True,
        "data": {
            "success": True,  # Frontend checks this for merge result display
            "merged": True,
            "message": message,
            "taskStatus": "done" if not status_error else "unchanged",
            **({"statusError": status_error} if status_error else {}),
            **extra,
        },
    }


def _merge_pull_request(project_path: Path, branch: str) -> tuple[bool, str]:
    """Merge the open PR for *branch*. Returns ``(merged, detail)``.

    ``(True, detail)``  -- merged now, or ALREADY merged. Idempotent on
                           purpose: Approve must not fail because someone
                           merged it first, or because it is clicked twice.
    ``(False, detail)`` -- a PR exists and could not be merged; detail says why.
    ``(False, "")``     -- no PR for this branch; the caller may fall back.
    """
    from .github import run_gh_command  # noqa: PLC0415 - avoids an import cycle

    cwd = str(project_path)
    found = run_gh_command(
        [
            "pr", "list", "--head", branch, "--state", "all",
            "--limit", "1", "--json", "number,state",
        ],
        cwd=cwd,
    )
    if not found.get("success"):
        return False, ""
    try:
        prs = json.loads(found.get("output") or "[]")
    except ValueError:
        return False, ""
    if not prs:
        return False, ""

    number = prs[0].get("number")
    state = (prs[0].get("state") or "").upper()
    if state == "MERGED":
        return True, f"pull request #{number} was already merged"
    if state == "CLOSED":
        return False, f"pull request #{number} is closed; reopen it to merge"

    merged = run_gh_command(["pr", "merge", str(number), "--squash"], cwd=cwd)
    if merged.get("success"):
        return True, f"merged pull request #{number}"
    return False, f"could not merge pull request #{number}: {merged.get('error')}"


class WorktreeMergeOptions(BaseModel):
    noCommit: bool | None = False


class ConflictResolveOptions(BaseModel):
    """Options for conflict resolution."""

    useAI: bool = True
    strategy: str | None = None


@router.get("/{task_id}/worktree/merge-preview")
async def get_worktree_merge_preview(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Preview what will happen when merging the worktree.
    Returns conflict info and files that will be merged.
    """
    import subprocess

    # Find the task's spec directory and worktree
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Find the task across all projects
    task_info = None
    project_path = None

    # Handle both dict format (id -> project) and list format
    if isinstance(projects_data, dict):
        projects = list(projects_data.values())
    else:
        projects = projects_data

    for project in projects:
        if isinstance(project, str):
            project_path = Path(project)
        else:
            project_path = Path(project.get("path", ""))

        spec_dir = project_path / ".aifactory" / "specs" / task_id

        if spec_dir.exists():
            # Found the task
            impl_plan = spec_dir / "implementation_plan.json"
            if impl_plan.exists():
                task_info = json.loads(impl_plan.read_text())
            break
    else:
        return {"success": False, "error": f"Task {task_id} not found"}

    # Find the worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / task_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get the branch name from the worktree
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not determine worktree branch"}

    # Get the base branch (usually develop or main)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Get list of changed files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        changed_files = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    status = parts[0]
                    filename = parts[1]
                    changed_files.append(
                        {
                            "path": filename,
                            "status": "added"
                            if status == "A"
                            else "modified"
                            if status == "M"
                            else "deleted"
                            if status == "D"
                            else status,
                        }
                    )
    except subprocess.CalledProcessError:
        changed_files = []

    # Check for potential conflicts using merge-tree (dry run)
    # Git 2.38+ uses new merge-tree format with --write-tree mode by default
    has_conflicts = False
    conflicting_files = []
    try:
        # Use --write-tree explicitly for git 2.38+ behavior
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", base_branch, worktree_branch],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        # Git 2.38+: Return code 1 means conflicts exist
        # stdout format: "<tree_oid>\nCONFLICT (type): description"
        if result.returncode == 1:
            has_conflicts = True
            # Parse CONFLICT lines to get conflicting files
            for line in result.stdout.split("\n"):
                if line.startswith("CONFLICT"):
                    # Extract filename from "CONFLICT (content): Merge conflict in path/file"
                    if " in " in line:
                        file_path = line.split(" in ")[-1].strip()
                        if file_path:
                            conflicting_files.append(file_path)
        # Fallback: Check for CONFLICT keyword even on return code 0
        # (some edge cases may not set return code correctly)
        elif "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
            has_conflicts = True
            for line in (result.stdout + result.stderr).split("\n"):
                if line.startswith("CONFLICT") and " in " in line:
                    file_path = line.split(" in ")[-1].strip()
                    if file_path:
                        conflicting_files.append(file_path)
        # Legacy fallback: Check for conflict markers (older git versions < 2.38)
        elif "<<<<<<" in result.stdout:
            has_conflicts = True
    except subprocess.CalledProcessError as e:
        # Command failed - check output for conflict indicators
        output = (e.stdout or "") + (e.stderr or "")
        if "CONFLICT" in output or "<<<<<<" in output:
            has_conflicts = True
            for line in output.split("\n"):
                if line.startswith("CONFLICT") and " in " in line:
                    file_path = line.split(" in ")[-1].strip()
                    if file_path:
                        conflicting_files.append(file_path)

    # Filter out gitignored files from conflict list (e.g. build artifacts)
    if conflicting_files:
        try:
            result = subprocess.run(
                ["git", "check-ignore"] + conflicting_files,
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            ignored = set(result.stdout.strip().splitlines())
            conflicting_files = [f for f in conflicting_files if f not in ignored]
            if not conflicting_files:
                has_conflicts = False
        except Exception:
            pass  # If check-ignore fails, keep the original list

    # Check if there's an active merge in progress (MERGE_HEAD exists)
    # This is different from the merge-tree dry run above - this means a real merge
    # is in progress with unresolved conflict markers in files
    merge_in_progress = False
    merge_head_file = project_path / ".git" / "MERGE_HEAD"
    if merge_head_file.exists():
        merge_in_progress = True

    # Get commit counts
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_branch}..{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        commits_ahead = int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commits_ahead = 0

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{worktree_branch}..{base_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        commits_behind = int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commits_behind = 0

    # Detect uncommitted changes in the main project that could conflict
    uncommitted_files = []
    uncommitted_conflicting_files = []
    try:
        # Get uncommitted files in main project
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                # Format: "XY filename" or "XY original -> renamed"
                parts = line[3:].split(" -> ")
                filename = parts[-1].strip()  # Use renamed name if present
                if filename:
                    uncommitted_files.append(filename)

        # Get files modified in task branch (for conflict detection)
        if uncommitted_files:
            task_files_result = subprocess.run(
                ["git", "diff", "--name-only", f"{base_branch}...{worktree_branch}"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if task_files_result.returncode == 0:
                task_files = set(task_files_result.stdout.strip().split("\n"))
                # Find files that overlap (uncommitted in main AND modified in task)
                uncommitted_conflicting_files = list(
                    set(uncommitted_files) & task_files
                )

                # Filter out gitignored files (e.g. build artifacts)
                if uncommitted_conflicting_files:
                    try:
                        ignored_result = subprocess.run(
                            ["git", "check-ignore"] + uncommitted_conflicting_files,
                            cwd=project_path,
                            capture_output=True,
                            text=True,
                        )
                        ignored = set(ignored_result.stdout.strip().splitlines())
                        uncommitted_conflicting_files = [
                            f for f in uncommitted_conflicting_files if f not in ignored
                        ]
                    except Exception:
                        pass
    except subprocess.CalledProcessError:
        pass  # Non-fatal - continue without uncommitted detection

    # Run semantic conflict detection using backend merge system
    semantic_conflicts = []
    semantic_stats = {
        "totalFiles": len(changed_files),
        "conflictFiles": 0,
        "totalConflicts": 0,
        "autoMergeable": 0,
        "aiResolved": 0,
        "humanRequired": 0,
    }

    try:
        from ..services.conflict_service import get_conflict_service

        conflict_service = get_conflict_service(project_path)
        semantic_result = await conflict_service.detect_conflicts(
            task_id=task_id,
            worktree_path=worktree_path,
            base_branch=base_branch,
        )

        if semantic_result.get("success"):
            semantic_conflicts = semantic_result.get("conflicts", [])
            semantic_stats = semantic_result.get("stats", semantic_stats)

    except Exception as e:
        # Log but don't fail - semantic detection is optional enhancement
        import logging

        logging.getLogger(__name__).warning(f"Semantic conflict detection failed: {e}")

    # Merge results: combine git conflicts with semantic conflicts
    all_conflicts = semantic_conflicts.copy()

    # Determine overall merge status
    total_conflicts = len(all_conflicts)
    auto_mergeable = sum(1 for c in all_conflicts if c.get("canAutoMerge", False))
    has_any_conflicts = has_conflicts or total_conflicts > 0
    can_merge = not has_conflicts and (
        total_conflicts == 0 or total_conflicts == auto_mergeable
    )

    # Build preview response with all merge information
    preview_data = {
        "files": [f["path"] for f in changed_files],
        "conflicts": all_conflicts,  # Semantic conflicts from merge system
        "summary": {
            "totalFiles": len(changed_files),
            "conflictFiles": semantic_stats.get("conflictFiles", 0),
            "totalConflicts": total_conflicts,
            "autoMergeable": auto_mergeable,
            "aiResolved": semantic_stats.get("aiResolved", 0),
            "humanRequired": total_conflicts - auto_mergeable,
        },
        "gitConflicts": {
            "hasConflicts": has_conflicts,
            "commitsAhead": commits_ahead,
            "commitsBehind": commits_behind,
            "conflictingFiles": conflicting_files,
            "needsRebase": commits_behind > 0,
            "baseBranch": base_branch,
            "specBranch": worktree_branch,
            "mergeInProgress": merge_in_progress,
        },
        "uncommittedChanges": {
            "hasChanges": len(uncommitted_files) > 0,
            "files": uncommitted_files,
            "count": len(uncommitted_files),
            "conflictingFiles": uncommitted_conflicting_files,
            "hasConflicts": len(uncommitted_conflicting_files) > 0,
        }
        if uncommitted_files
        else None,
    }

    return {
        "success": True,
        "data": {
            "canMerge": can_merge,
            "hasConflicts": has_any_conflicts,
            "changedFiles": changed_files,
            "conflicts": all_conflicts,
            "stats": preview_data["summary"],
            "gitConflicts": preview_data["gitConflicts"],
            "worktreeBranch": worktree_branch,
            "baseBranch": base_branch,
            "preview": preview_data,
        },
    }


@router.post("/{task_id}/worktree/resolve-conflicts")
async def resolve_worktree_conflicts(
    task_id: str,
    options: ConflictResolveOptions = None,
    _access: dict = Depends(require_task_access("member")),
):
    """
    Resolve merge conflicts between the worktree branch and the base branch using AI.

    This endpoint performs a real git merge and resolves any conflict markers
    using AI. Process:
    1. Gets the worktree branch name
    2. Starts a git merge of the worktree branch into the current branch
    3. If conflicts arise, uses AI to resolve each conflicted file
    4. Stages resolved files and commits the merge
    """
    import logging

    logger = logging.getLogger(__name__)

    if options is None:
        options = ConflictResolveOptions()

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Barrier BEFORE spec_id reaches any path expression (#1056).
        try:
            spec_id = safe_spec_component(spec_id)
        except ValueError:
            return {"success": False, "error": "Invalid task ID format"}
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not spec_dir.exists():
        return {"success": False, "error": f"Task {task_id} not found"}

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get worktree branch name
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not determine worktree branch"}

    # Check if a merge is already in progress
    merge_head = project_path / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        logger.info(
            f"Merge already in progress for {task_id}, resolving existing conflicts"
        )
    else:
        # Start the git merge (allow conflicts)
        logger.info(
            f"Starting git merge of {worktree_branch} into current branch for task {task_id}"
        )
        merge_result = subprocess.run(
            ["git", "merge", worktree_branch, "--no-commit", "--no-ff"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        if merge_result.returncode == 0:
            # Clean merge, no conflicts - commit it
            logger.info(f"Clean merge for {task_id}, committing")
            commit_result = subprocess.run(
                ["git", "commit", "-m", f"Merge {worktree_branch} into current branch"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            return {
                "success": True,
                "data": {
                    "resolved": [],
                    "remaining": [],
                    "stats": {"message": "Clean merge - no conflicts"},
                },
            }
        elif merge_result.returncode != 1 and "CONFLICT" not in merge_result.stdout:
            # Unexpected error (not a conflict)
            logger.error(f"Git merge failed unexpectedly: {merge_result.stderr}")
            return {
                "success": False,
                "error": f"Git merge failed: {merge_result.stderr.strip()}",
            }
        else:
            logger.info(f"Merge has conflicts for {task_id}, resolving with AI")

    if not options.useAI:
        return {
            "success": False,
            "error": "Conflicts detected but AI resolution is disabled",
            "data": {
                "resolved": [],
                "remaining": [],
                "stats": {
                    "message": "Merge started with conflicts, AI resolution disabled"
                },
            },
        }

    # Get list of conflicted files
    conflicted_files = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            conflicted_files = [f for f in result.stdout.strip().split("\n") if f]
    except subprocess.CalledProcessError:
        pass

    # Fallback: check git status for unmerged files
    if not conflicted_files:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line and line[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                        file_path = line[3:].strip()
                        if file_path:
                            conflicted_files.append(file_path)
        except subprocess.CalledProcessError:
            pass

    if not conflicted_files:
        # No conflicts found - the merge may have already been resolved
        # Try to commit
        commit_result = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        return {
            "success": True,
            "data": {
                "resolved": [],
                "remaining": [],
                "stats": {"message": "No conflicted files found"},
            },
        }

    logger.info(f"Found {len(conflicted_files)} conflicted files: {conflicted_files}")

    # Resolve each conflicted file using AI
    resolved_files = []
    failed_files = []

    from ..services.conflict_service import get_conflict_service

    conflict_service = get_conflict_service(project_path)

    for file_path in conflicted_files:
        try:
            full_path = project_path / file_path
            if not full_path.exists():
                logger.warning(f"Conflicted file not found: {full_path}")
                failed_files.append({"file": file_path, "error": "File not found"})
                continue

            content = full_path.read_text()

            if "<<<<<<< " not in content:
                logger.info(f"File {file_path} has no conflict markers, staging")
                subprocess.run(
                    ["git", "add", file_path],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                )
                resolved_files.append(file_path)
                continue

            # Use AI to resolve conflict markers
            merge_result = await conflict_service.resolve_conflict_markers(
                file_path=file_path,
                content=content,
            )

            if merge_result.get("success"):
                resolved_content = merge_result.get("content", "")

                # Clean up any remaining markers
                if (
                    "<<<<<<< " in resolved_content
                    or "=======" in resolved_content
                    or ">>>>>>> " in resolved_content
                ):
                    logger.warning(
                        f"AI resolution for {file_path} still has markers, cleaning up"
                    )
                    resolved_content = _clean_conflict_markers(resolved_content)

                full_path.write_text(resolved_content)
                logger.info(f"Wrote resolved content to {full_path}")

                result = subprocess.run(
                    ["git", "add", file_path],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    resolved_files.append(file_path)
                    logger.info(f"Staged resolved file: {file_path}")
                else:
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": f"Failed to stage: {result.stderr}",
                        }
                    )
            else:
                error_msg = merge_result.get("error", "AI resolution failed")
                logger.error(f"AI resolution failed for {file_path}: {error_msg}")
                failed_files.append({"file": file_path, "error": error_msg})

        except Exception as e:
            logger.error(f"Failed to resolve {file_path}: {e}")
            failed_files.append({"file": file_path, "error": str(e)})

    if failed_files:
        return {
            "success": len(resolved_files) > 0,
            "data": {
                "resolved": resolved_files,
                "failed": failed_files,
                "remaining": [f["file"] for f in failed_files],
                "stats": {
                    "message": f"Resolved {len(resolved_files)} files, {len(failed_files)} failed"
                },
            },
            "error": f"{len(failed_files)} files could not be resolved",
        }

    # All conflicts resolved - commit the merge
    try:
        commit_msg = f"Merge {worktree_branch} (AI-resolved conflicts)"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Merge commit failed: {result.stderr}")
            return {
                "success": True,
                "data": {
                    "resolved": resolved_files,
                    "remaining": [],
                    "stats": {
                        "message": f"Resolved {len(resolved_files)} files but commit failed: {result.stderr.strip()}"
                    },
                },
            }
    except Exception as e:
        logger.warning(f"Merge commit failed: {e}")

    return {
        "success": True,
        "data": {
            "resolved": resolved_files,
            "remaining": [],
            "stats": {
                "message": f"Successfully resolved and merged {len(resolved_files)} conflicting files"
            },
        },
    }


@router.post("/{task_id}/worktree/resolve-uncommitted")
async def resolve_uncommitted_conflicts(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """
    Resolve conflicts between uncommitted local changes and task branch changes using AI.

    This endpoint:
    1. Stashes uncommitted changes in the main project
    2. For each conflicting file, gets the stash, task branch, and base versions
    3. Uses AI to intelligently merge the three versions
    4. Writes merged content to working directory
    5. Drops the stash after successful merge
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Resolving uncommitted conflicts for task {task_id}")

    # Find the task's project
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Find the task across all projects
    project_path = None
    worktree_path = None

    if isinstance(projects_data, dict):
        projects = list(projects_data.values())
    else:
        projects = projects_data

    for project in projects:
        if isinstance(project, str):
            project_path = Path(project)
        else:
            project_path = Path(project.get("path", ""))

        spec_dir = project_path / ".aifactory" / "specs" / task_id

        if spec_dir.exists():
            worktree_path = (
                project_path / ".aifactory" / "worktrees" / "tasks" / task_id
            )
            break
    else:
        return {"success": False, "error": f"Task {task_id} not found"}

    if not worktree_path or not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get branch names
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        spec_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not determine branches"}

    # Get uncommitted files that conflict with task
    uncommitted_files = []
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line[3:].split(" -> ")
                filename = parts[-1].strip()
                if filename:
                    uncommitted_files.append(filename)
    except subprocess.CalledProcessError:
        return {"success": False, "error": "Could not get uncommitted files"}

    # Get task branch files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...{spec_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        task_files = set(result.stdout.strip().split("\n"))
    except subprocess.CalledProcessError:
        task_files = set()

    # Find conflicting files
    conflicting_files = list(set(uncommitted_files) & task_files)

    if not conflicting_files:
        return {
            "success": True,
            "data": {"message": "No conflicting files found", "resolved": []},
        }

    # Stash uncommitted changes (include untracked files)
    stash_message = f"aifactory-temp-{task_id}"
    stash_created = False
    try:
        # First try with --include-untracked to catch new files
        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", stash_message],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and "No local changes to save" not in result.stdout:
            stash_created = True
            logger.info(f"Stashed changes: {result.stdout.strip()}")
        elif result.returncode != 0:
            # Fallback: try without --include-untracked (for older git or if no untracked)
            result = subprocess.run(
                ["git", "stash", "push", "-m", stash_message],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            if (
                result.returncode == 0
                and "No local changes to save" not in result.stdout
            ):
                stash_created = True
                logger.info(f"Stashed changes (fallback): {result.stdout.strip()}")
            elif result.returncode != 0 and "No local changes to save" not in (
                result.stderr + result.stdout
            ):
                return {
                    "success": False,
                    "error": f"Failed to stash changes: {result.stderr or result.stdout}",
                }
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": f"Failed to stash changes: {e.stderr}"}

    resolved_files = []
    failed_files = []

    try:
        for file_path in conflicting_files:
            try:
                # Get base version (from base branch)
                base_content = ""
                try:
                    result = subprocess.run(
                        ["git", "show", f"{base_branch}:{file_path}"],
                        cwd=project_path,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        base_content = result.stdout
                except Exception:
                    pass

                # Get local version (uncommitted changes)
                # If we stashed, get from stash; otherwise read from working directory
                local_content = ""
                try:
                    if stash_created:
                        result = subprocess.run(
                            ["git", "show", f"stash@{{0}}:{file_path}"],
                            cwd=project_path,
                            capture_output=True,
                            text=True,
                        )
                        if result.returncode == 0:
                            local_content = result.stdout
                    else:
                        # Read directly from working directory
                        working_file = project_path / file_path
                        if working_file.exists():
                            local_content = working_file.read_text()
                except Exception:
                    pass

                # Get task branch version
                task_content = ""
                try:
                    result = subprocess.run(
                        ["git", "show", f"{spec_branch}:{file_path}"],
                        cwd=project_path,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        task_content = result.stdout
                except Exception:
                    pass

                # Use AI to merge the three versions
                from ..services.conflict_service import get_conflict_service

                conflict_service = get_conflict_service(project_path)
                merge_result = await conflict_service.ai_merge_three_way(
                    file_path=file_path,
                    base_content=base_content,
                    local_content=local_content,
                    task_content=task_content,
                    local_label="your uncommitted changes",
                    task_label=f"task {task_id} changes",
                )

                if merge_result.get("success"):
                    # Write merged content to working directory
                    full_path = project_path / file_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(merge_result.get("content", ""))
                    resolved_files.append(file_path)
                else:
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": merge_result.get("error", "Unknown error"),
                        }
                    )

            except Exception as e:
                logger.error(f"Failed to resolve {file_path}: {e}")
                failed_files.append({"file": file_path, "error": str(e)})

    finally:
        # Drop the stash only if we created one
        if stash_created:
            try:
                subprocess.run(
                    ["git", "stash", "drop"],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                )
                logger.info("Dropped stash after merge")
            except Exception:
                logger.warning("Failed to drop stash - may need manual cleanup")

    if failed_files:
        return {
            "success": len(resolved_files) > 0,
            "data": {
                "resolved": resolved_files,
                "failed": failed_files,
                "message": f"Resolved {len(resolved_files)} files, {len(failed_files)} failed",
            },
            "error": f"{len(failed_files)} files could not be resolved",
        }

    return {
        "success": True,
        "data": {
            "resolved": resolved_files,
            "failed": [],
            "message": f"Successfully resolved {len(resolved_files)} conflicting files",
        },
    }


@router.post("/{task_id}/worktree/resolve-git-merge")
async def resolve_git_merge_conflicts(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """
    Resolve files with git merge conflict markers using AI.

    This endpoint handles the case where a git merge is in progress and files
    contain conflict markers (<<<<<<< HEAD, =======, >>>>>>> branch).

    Unlike resolve_uncommitted_conflicts (which uses stash), this works directly
    with files that already have conflict markers from an in-progress merge.

    Process:
    1. Check if merge is in progress (.git/MERGE_HEAD exists)
    2. Get list of unresolved conflicted files
    3. For each file, use AI to resolve the conflict markers
    4. Write resolved content and stage the file
    5. Return success (user can then commit the merge)
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Resolving git merge conflicts for task {task_id}")

    # Find the task's project
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Find the task across all projects
    project_path = None
    worktree_path = None

    if isinstance(projects_data, dict):
        projects = list(projects_data.values())
    else:
        projects = projects_data

    for project in projects:
        if isinstance(project, str):
            project_path = Path(project)
        else:
            project_path = Path(project.get("path", ""))

        spec_dir = project_path / ".aifactory" / "specs" / task_id

        if spec_dir.exists():
            worktree_path = (
                project_path / ".aifactory" / "worktrees" / "tasks" / task_id
            )
            break
    else:
        return {"success": False, "error": f"Task {task_id} not found"}

    # Determine which path to work with (main project or worktree)
    # Check both locations for merge in progress
    work_path = None
    merge_head_main = project_path / ".git" / "MERGE_HEAD"
    merge_head_worktree = (
        worktree_path / ".git" if worktree_path and worktree_path.exists() else None
    )

    if merge_head_main.exists():
        work_path = project_path
        logger.info(f"Found merge in progress in main project: {project_path}")
    elif merge_head_worktree and (merge_head_worktree / "MERGE_HEAD").exists():
        work_path = worktree_path
        logger.info(f"Found merge in progress in worktree: {worktree_path}")
    else:
        # No merge in progress - check if there are files with conflict markers anyway
        # This can happen if the merge state was cleared but files still have markers
        logger.info("No MERGE_HEAD found, checking for conflict markers in files...")
        work_path = project_path  # Default to main project

    # Get list of files with unresolved conflicts
    conflicted_files = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=work_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            conflicted_files = [f for f in result.stdout.strip().split("\n") if f]
            logger.info(
                f"Found {len(conflicted_files)} conflicted files: {conflicted_files}"
            )
    except subprocess.CalledProcessError as e:
        logger.warning(f"git diff --diff-filter=U failed: {e}")

    # If no conflicted files from git, scan for files with conflict markers
    if not conflicted_files:
        logger.info(
            "No files from git diff --diff-filter=U, scanning for conflict markers..."
        )
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=work_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line and (
                        line.startswith("UU")
                        or line.startswith("AA")
                        or line.startswith("DD")
                        or line.startswith("AU")
                        or line.startswith("UA")
                        or line.startswith("DU")
                        or line.startswith("UD")
                    ):
                        # Status codes indicate conflicts
                        file_path = line[3:].strip()
                        if file_path:
                            conflicted_files.append(file_path)
        except subprocess.CalledProcessError:
            pass

    if not conflicted_files:
        return {
            "success": True,
            "data": {
                "resolved": [],
                "failed": [],
                "message": "No conflicted files found",
            },
        }

    # Resolve each conflicted file using AI
    resolved_files = []
    failed_files = []

    from ..services.conflict_service import get_conflict_service

    conflict_service = get_conflict_service(project_path)

    for file_path in conflicted_files:
        try:
            full_path = work_path / file_path
            if not full_path.exists():
                logger.warning(f"Conflicted file not found: {full_path}")
                failed_files.append({"file": file_path, "error": "File not found"})
                continue

            # Read file content with conflict markers
            content = full_path.read_text()

            # Check if file actually has conflict markers
            if "<<<<<<< " not in content:
                logger.info(f"File {file_path} has no conflict markers, skipping")
                # Stage it anyway since git thinks it's conflicted
                subprocess.run(
                    ["git", "add", file_path],
                    cwd=work_path,
                    capture_output=True,
                    text=True,
                )
                resolved_files.append(file_path)
                continue

            # Use AI to resolve conflict markers
            merge_result = await conflict_service.resolve_conflict_markers(
                file_path=file_path,
                content=content,
            )

            if merge_result.get("success"):
                resolved_content = merge_result.get("content", "")

                # Verify no conflict markers remain
                if (
                    "<<<<<<< " in resolved_content
                    or "=======" in resolved_content
                    or ">>>>>>> " in resolved_content
                ):
                    logger.warning(
                        f"AI resolution for {file_path} still contains conflict markers"
                    )
                    # Try to clean up obvious marker remnants
                    resolved_content = _clean_conflict_markers(resolved_content)

                # Write resolved content
                full_path.write_text(resolved_content)
                logger.info(f"Wrote resolved content to {full_path}")

                # Stage the file
                result = subprocess.run(
                    ["git", "add", file_path],
                    cwd=work_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    resolved_files.append(file_path)
                    logger.info(f"Staged resolved file: {file_path}")
                else:
                    logger.warning(f"Failed to stage {file_path}: {result.stderr}")
                    failed_files.append(
                        {
                            "file": file_path,
                            "error": f"Failed to stage: {result.stderr}",
                        }
                    )
            else:
                error_msg = merge_result.get("error", "AI resolution failed")
                logger.error(f"AI resolution failed for {file_path}: {error_msg}")
                failed_files.append({"file": file_path, "error": error_msg})

        except Exception as e:
            logger.error(f"Failed to resolve {file_path}: {e}")
            failed_files.append({"file": file_path, "error": str(e)})

    if failed_files:
        return {
            "success": len(resolved_files) > 0,
            "data": {
                "resolved": resolved_files,
                "failed": failed_files,
                "message": f"Resolved {len(resolved_files)} files, {len(failed_files)} failed",
            },
            "error": f"{len(failed_files)} files could not be resolved",
        }

    # All conflicts resolved successfully - auto-commit the merge
    commit_result = None
    try:
        # Get the branch being merged for the commit message
        merge_head_file = work_path / ".git" / "MERGE_HEAD"
        merge_branch = "task branch"
        if merge_head_file.exists():
            merge_commit = merge_head_file.read_text().strip()[:8]
            # Try to get branch name from the merge
            result = subprocess.run(
                ["git", "name-rev", "--name-only", merge_commit],
                cwd=work_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                merge_branch = result.stdout.strip()

        # Commit the merge
        commit_msg = f"Merge {merge_branch} (AI-resolved conflicts)"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=work_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            commit_result = "Merge committed successfully"
            logger.info(f"Auto-committed merge: {commit_msg}")
        else:
            commit_result = f"Commit failed: {result.stderr}"
            logger.warning(f"Failed to auto-commit merge: {result.stderr}")
    except Exception as e:
        commit_result = f"Commit error: {e!s}"
        logger.error(f"Error during auto-commit: {e}")

    return {
        "success": True,
        "data": {
            "resolved": resolved_files,
            "failed": [],
            "message": f"Successfully resolved {len(resolved_files)} conflicted files",
            "commit": commit_result,
        },
    }


def _clean_conflict_markers(content: str) -> str:
    """
    Clean up any remaining conflict markers from content.
    This is a fallback if AI resolution leaves some markers.
    """
    import re

    # Pattern to match conflict blocks
    # <<<<<<< ... ======= ... >>>>>>>
    pattern = r"<<<<<<<[^\n]*\n(.*?)=======\n(.*?)>>>>>>>[^\n]*\n?"

    def replace_conflict(match):
        # Prefer the second version (usually "theirs"/incoming changes)
        # This is a simple heuristic - the AI should have already merged properly
        ours = match.group(1)
        theirs = match.group(2)
        # If theirs is empty, use ours
        if not theirs.strip():
            return ours
        return theirs

    cleaned = re.sub(pattern, replace_conflict, content, flags=re.DOTALL)
    return cleaned


@router.post("/{task_id}/worktree/abort-merge")
async def abort_worktree_merge(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """
    Abort a failed merge in the worktree or main project.

    This resets the git state when a merge has left the repository in an
    unmerged/conflicted state. It runs `git merge --abort` in both the
    worktree and the main project to ensure a clean state.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Aborting merge for task {task_id}")

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Barrier BEFORE spec_id reaches any path expression (#1056).
        try:
            spec_id = safe_spec_component(spec_id)
        except ValueError:
            return {"success": False, "error": "Invalid task ID format"}
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        return {"success": False, "error": f"Task {task_id} not found"}

    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    aborted_locations = []
    errors = []

    # Try to abort merge in worktree first
    if worktree_path and worktree_path.exists():
        try:
            # Check if worktree is in a merge state
            merge_head = worktree_path / ".git" / "MERGE_HEAD"
            if merge_head.exists() or (worktree_path / "MERGE_HEAD").exists():
                result = subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    aborted_locations.append("worktree")
                    logger.info(f"Aborted merge in worktree: {worktree_path}")
                else:
                    logger.warning(
                        f"Failed to abort merge in worktree: {result.stderr}"
                    )
                    errors.append(f"Worktree: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append("Worktree: git merge --abort timed out")
        except Exception as e:
            logger.error(f"Error aborting merge in worktree: {e}")
            errors.append(f"Worktree: {e!s}")

    # Try to abort merge in main project
    if project_path and project_path.exists():
        try:
            # Check if main project is in a merge state
            git_dir = project_path / ".git"
            merge_head = (
                git_dir / "MERGE_HEAD"
                if git_dir.is_dir()
                else project_path / ".git" / "MERGE_HEAD"
            )
            if merge_head.exists():
                result = subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    aborted_locations.append("main project")
                    logger.info(f"Aborted merge in main project: {project_path}")
                else:
                    logger.warning(
                        f"Failed to abort merge in main project: {result.stderr}"
                    )
                    errors.append(f"Main project: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append("Main project: git merge --abort timed out")
        except Exception as e:
            logger.error(f"Error aborting merge in main project: {e}")
            errors.append(f"Main project: {e!s}")

    if aborted_locations:
        return {
            "success": True,
            "data": {
                "abortedIn": aborted_locations,
                "message": f"Merge aborted in: {', '.join(aborted_locations)}",
            },
        }
    elif errors:
        return {"success": False, "error": "; ".join(errors)}
    else:
        return {
            "success": True,
            "data": {"abortedIn": [], "message": "No active merge found to abort"},
        }


@router.post("/{task_id}/worktree/merge")
async def merge_worktree(
    task_id: str,
    options: WorktreeMergeOptions = None,
    _access: dict = Depends(require_task_access("member")),
):
    """
    Merge the worktree branch into the base branch.
    """
    import logging
    import subprocess

    # This handler uses ``logger`` (e.g. when clearing internal merge-blocking
    # files) but — unlike its sibling handlers — never bound it, so that path
    # raised NameError → HTTP 500 on Approve (PR + merge). Bind it locally,
    # matching the per-function pattern used elsewhere in this module.
    logger = logging.getLogger(__name__)

    if options is None:
        options = WorktreeMergeOptions()

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Barrier BEFORE spec_id reaches any path expression (#1056).
        try:
            spec_id = safe_spec_component(spec_id)
        except ValueError:
            return {"success": False, "error": "Invalid task ID format"}
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        return {"success": False, "error": f"Task {task_id} not found"}

    # Find the worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get the branch name from the worktree
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # #1073: same defect as create-pr -- the worktree's HEAD is the base branch
    # under the kubejob backend, so merging it would have been a no-op merge of
    # main into main. Resolve the branch that actually holds the work.
    worktree_branch, branch_error = resolve_task_branch(
        worktree_path=worktree_path,
        project_path=project_path,
        spec_id=spec_id,
        base_branch=base_branch,
    )
    if not worktree_branch:
        return {"success": False, "error": f"Could not determine task branch: {branch_error}"}

    # Clean up internal auto-generated files that can block merge
    # These are untracked files created by agents in worktrees that would
    # collide with the same untracked files in the main working directory.
    _INTERNAL_MERGE_BLOCKERS = [
        ".aifactory-security.json",
        ".aifactory-status",
    ]
    for fname in _INTERNAL_MERGE_BLOCKERS:
        blocker = project_path / fname
        if blocker.exists():
            try:
                blocker.unlink()
                logger.info(f"Removed merge-blocking file: {fname}")
            except OSError:
                pass

    # #1076: merge the PULL REQUEST, not a local branch, when one exists.
    #
    # The local `git merge` below updates the control plane's own checkout and
    # never pushes -- there is not one push in this handler -- so GitHub never
    # saw the result and the PR stayed open. Under the kubejob build backend it
    # cannot even do that: the branch lives in the build's separate clone and on
    # origin, so `git merge <branch>` in the project repo fails with "not
    # something we can merge".
    #
    # The PR is the artefact the button's own label refers to, exists for both
    # build backends, and merging it is visible to everyone rather than to one
    # pod's filesystem.
    pr_merged, pr_detail = _merge_pull_request(project_path, worktree_branch)
    if pr_merged:
        return _approved(project_path, spec_id, pr_detail)
    if pr_detail:
        # A PR exists and GitHub REFUSED the merge (conflicts, required checks,
        # branch protection). Falling through to a local merge would report
        # success for something GitHub declined.
        return {"success": False, "error": pr_detail}

    # No PR for this branch: the historical local-merge path.
    try:
        merge_cmd = ["git", "merge", worktree_branch]
        if options.noCommit:
            merge_cmd.append("--no-commit")

        result = subprocess.run(
            merge_cmd, cwd=project_path, capture_output=True, text=True, check=True
        )

        # Clean up worktree after successful merge
        worktree_deleted = False
        branch_deleted = False
        try:
            # Remove git worktree
            cleanup_result = subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            worktree_deleted = cleanup_result.returncode == 0

            # Delete the branch (it's merged now)
            branch_result = subprocess.run(
                ["git", "branch", "-d", worktree_branch],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
            branch_deleted = branch_result.returncode == 0
        except Exception as e:
            logger.warning(f"Failed to cleanup worktree after merge: {e}")
            # Don't fail the merge just because cleanup failed

        # #1071: the merge IS the approval -- see _approved. Success path only:
        # a conflict or a failed merge returns below and must never be recorded
        # as done.
        return _approved(
            project_path,
            spec_id,
            f"Successfully merged {worktree_branch} into {base_branch}",
            output=result.stdout,
            worktreeDeleted=worktree_deleted,
            branchDeleted=branch_deleted,
        )

    except subprocess.CalledProcessError as e:
        # Check if it's a conflict
        if "CONFLICT" in e.stdout or "CONFLICT" in e.stderr:
            return {
                "success": False,
                "error": "Merge conflicts detected. Please resolve manually.",
                "conflicts": True,
                "output": e.stdout + e.stderr,
            }
        return {
            "success": False,
            "error": f"Merge failed: {e.stderr or e.stdout}",
            "output": e.stdout + e.stderr,
        }


@router.get("/{task_id}/worktree/status")
async def get_worktree_status(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Get the status of a task's worktree.
    Returns information about the worktree including changed files count,
    additions/deletions, and whether it exists.
    """
    import subprocess

    # Parse task_id to get project_id and spec_id
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
    else:
        # task_id is just the spec_id, search for project
        spec_id = task_id
        project_id = None

    # Barrier AFTER the if/else, so it covers BOTH branches (#1056) - the
    # bare-id branch carries request data just the same. This endpoint reports
    # existence, so a component that could never name a real spec reports
    # "does not exist" rather than erroring.
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        return {"success": True, "data": {"exists": False}}

    # Find project path
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {
            "success": True,
            "data": {
                "exists": False,
            },
        }

    projects_data = json.loads(projects_file.read_text())

    # Handle both dict format (id -> project) and list format
    project_path = None
    if isinstance(projects_data, dict):
        if project_id and project_id in projects_data:
            project_path = Path(projects_data[project_id]["path"])
        else:
            # Search all projects for this spec
            for proj in projects_data.values():
                path = Path(proj["path"])
                if (path / ".aifactory" / "specs" / spec_id).exists():
                    project_path = path
                    break
    else:
        for project in projects_data:
            path = Path(project.get("path", ""))
            if (project_id and project.get("id") == project_id) or (path / ".aifactory" / "specs" / spec_id).exists():
                project_path = path
                break

    if not project_path:
        return {
            "success": True,
            "data": {
                "exists": False,
            },
        }

    # Check for worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {
            "success": True,
            "data": {
                "exists": False,
            },
        }

    # Get worktree branch
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        worktree_branch = f"aifactory/{spec_id}"

    # Get base branch from main project
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Count commits ahead
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_branch}..{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_count = int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commit_count = 0

    # Get changed files stats
    files_changed = 0
    additions = 0
    deletions = 0

    try:
        result = subprocess.run(
            ["git", "diff", "--stat", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse the last line for summary (e.g., "5 files changed, 100 insertions(+), 20 deletions(-)")
        lines = result.stdout.strip().split("\n")
        if lines:
            summary_line = lines[-1]
            import re

            files_match = re.search(r"(\d+) files? changed", summary_line)
            if files_match:
                files_changed = int(files_match.group(1))
            insert_match = re.search(r"(\d+) insertions?\(\+\)", summary_line)
            if insert_match:
                additions = int(insert_match.group(1))
            del_match = re.search(r"(\d+) deletions?\(-\)", summary_line)
            if del_match:
                deletions = int(del_match.group(1))
    except subprocess.CalledProcessError:
        pass

    return {
        "success": True,
        "data": {
            "exists": True,
            "worktreePath": str(worktree_path),
            "branch": worktree_branch,
            "baseBranch": base_branch,
            "commitCount": commit_count,
            "filesChanged": files_changed,
            "additions": additions,
            "deletions": deletions,
        },
    }


@router.get("/{task_id}/worktree/diff")
async def get_worktree_diff(
    task_id: str, _access: dict = Depends(require_task_access("viewer"))
):
    """
    Get the diff details for a task's worktree.
    Returns detailed file-by-file changes between the worktree branch and base branch.
    """
    import subprocess

    # Parse task_id to get project_id and spec_id
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Barrier BEFORE spec_id reaches any path expression (#1056).
        try:
            spec_id = safe_spec_component(spec_id)
        except ValueError:
            return {"success": False, "error": "Invalid task ID format"}
    else:
        spec_id = task_id
        project_id = None

    # Find project path
    projects_data_dir = get_data_dir()
    projects_file = projects_data_dir / "projects.json"

    if not projects_file.exists():
        return {"success": False, "error": "No projects configured"}

    projects_data = json.loads(projects_file.read_text())

    # Handle both dict format (id -> project) and list format
    project_path = None
    if isinstance(projects_data, dict):
        if project_id and project_id in projects_data:
            project_path = Path(projects_data[project_id]["path"])
        else:
            for proj in projects_data.values():
                path = Path(proj["path"])
                if (path / ".aifactory" / "specs" / spec_id).exists():
                    project_path = path
                    break
    else:
        for project in projects_data:
            path = Path(project.get("path", ""))
            if (project_id and project.get("id") == project_id) or (path / ".aifactory" / "specs" / spec_id).exists():
                project_path = path
                break

    if not project_path:
        return {"success": False, "error": f"Project not found for task {task_id}"}

    # Check for worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    # Get worktree branch
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktree_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        worktree_branch = f"aifactory/{spec_id}"

    # Get base branch from main project
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip()
    except subprocess.CalledProcessError:
        base_branch = "develop"

    # Get detailed diff with numstat
    files = []
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if len(parts) >= 3:
                    added = parts[0]
                    deleted = parts[1]
                    path = parts[2]
                    # Handle binary files (show as -)
                    additions = int(added) if added != "-" else 0
                    deletions = int(deleted) if deleted != "-" else 0
                    files.append(
                        {
                            "path": path,
                            "status": "modified",  # Will be refined below
                            "additions": additions,
                            "deletions": deletions,
                        }
                    )
    except subprocess.CalledProcessError:
        pass

    # Get file statuses (A/M/D/R)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", f"{base_branch}...{worktree_branch}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        status_map = {}
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    status_code = parts[0][0]  # First char (R100 -> R)
                    filename = parts[-1]  # Last part is the filename
                    status = "modified"
                    if status_code == "A":
                        status = "added"
                    elif status_code == "D":
                        status = "deleted"
                    elif status_code == "R":
                        status = "renamed"
                    elif status_code == "M":
                        status = "modified"
                    status_map[filename] = status

        # Update files with proper status
        for f in files:
            if f["path"] in status_map:
                f["status"] = status_map[f["path"]]
    except subprocess.CalledProcessError:
        pass

    # Filter out internal aifactory files and agent artifacts (not relevant for user review)
    INTERNAL_FILES = {".aifactory-security.json", ".aifactory-status"}
    INTERNAL_PREFIXES = (".aifactory/", "VERIFICATION_REPORT", "LANGUAGE_CHOICE")
    files = [
        f
        for f in files
        if f["path"] not in INTERNAL_FILES
        and not any(f["path"].startswith(p) for p in INTERNAL_PREFIXES)
    ]

    # Fallback: if git diff shows no user-facing files but worktree has changes,
    # list files that exist in worktree but not in the main project
    if not files and worktree_path.exists():
        for f in worktree_path.iterdir():
            # Skip internal files, directories, and dotfiles
            if f.name.startswith(".") or f.name.startswith("__") or f.is_dir():
                continue
            if f.name in INTERNAL_FILES or any(
                f.name.startswith(p) for p in INTERNAL_PREFIXES
            ):
                continue
            # Check if this file exists in the main project
            main_file = project_path / f.name
            if not main_file.exists():
                # New file created by the agent
                try:
                    content = f.read_text(errors="replace")
                    line_count = content.count("\n") + (
                        1 if content and not content.endswith("\n") else 0
                    )
                    # Generate a unified diff for display
                    diff_lines = ["--- /dev/null", f"+++ b/{f.name}"]
                    diff_lines.append(f"@@ -0,0 +1,{line_count} @@")
                    for line in content.splitlines():
                        diff_lines.append(f"+{line}")
                    synthetic_diff = "\n".join(diff_lines) + "\n"
                except OSError:
                    line_count = 0
                    synthetic_diff = ""
                files.append(
                    {
                        "path": f.name,
                        "status": "added",
                        "additions": line_count,
                        "deletions": 0,
                        "diff": synthetic_diff,
                    }
                )

    # Get actual diff content for each file
    for f in files:
        try:
            result = subprocess.run(
                ["git", "diff", f"{base_branch}...{worktree_branch}", "--", f["path"]],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            f["diff"] = result.stdout
        except subprocess.CalledProcessError:
            # If diff fails for a file, leave diff empty
            f["diff"] = ""

    # Generate summary
    total_additions = sum(f["additions"] for f in files)
    total_deletions = sum(f["deletions"] for f in files)
    summary = f"{len(files)} files changed, +{total_additions} -{total_deletions}"

    return {
        "success": True,
        "data": {
            "files": files,
            "summary": summary,
        },
    }


@router.post("/{task_id}/worktree/discard")
async def discard_worktree(
    task_id: str, _access: dict = Depends(require_task_access("admin"))
):
    """
    Discard/delete the worktree for a task.
    Removes the worktree directory and optionally the branch.
    """
    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Barrier BEFORE spec_id reaches any path expression (#1056).
        try:
            spec_id = safe_spec_component(spec_id)
        except ValueError:
            return {"success": False, "error": "Invalid task ID format"}
        # Look up project path
        projects_file = get_projects_file()
        if not projects_file.exists():
            return {"success": False, "error": "Projects file not found"}

        import json

        projects_data = json.loads(projects_file.read_text())

        # Handle dict format where keys are project IDs
        if isinstance(projects_data, dict):
            project = projects_data.get(project_id)
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
        else:
            # Handle list format where each item has an "id" field
            project = None
            for p in projects_data:
                if isinstance(p, dict) and p.get("id") == project_id:
                    project = p
                    break
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            project_path = Path(project["path"])
    else:
        # task_id is just the spec_id, need to find project from context
        return {
            "success": False,
            "error": "Task ID must include project ID (format: project_id:spec_id)",
        }

    # Find the worktree
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id

    if not worktree_path.exists():
        return {"success": False, "error": "No worktree found for this task"}

    try:
        # Get the branch name before removing worktree
        branch_name = f"aifactory/{spec_id}"

        # Remove worktree using git command
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Fallback: force delete directory
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

        # Prune worktrees
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        # Delete the branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=project_path,
            capture_output=True,
            text=True,
        )

        return {
            "success": True,
            "data": {
                "discarded": True,
                "message": f"Successfully discarded worktree for {spec_id}",
            },
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to discard worktree: {e!s}"}
