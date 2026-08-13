"""
PR-creation route (push the worktree branch and open a GitHub/provider Pull Request).

Extracted verbatim from ``routes/tasks.py`` (issue #556) as a behavior-preserving
sub-router. The endpoint is mounted onto the tasks router via ``include_router``
so the public path and request/response shapes are unchanged:

    POST /{task_id}/worktree/create-pr

The handler depends only on already-extractable collaborators -- ``get_projects_file``
from ``routes/projects`` (whose ``tasks`` import is lazy, so no module-level cycle),
``require_task_access`` from ``routes/project_authz``, and the ``_get_project_provider``
/ ``_use_provider_api`` / ``run_gh_command`` helpers from ``routes/github`` (imported
lazily inside the handler). None of those modules import this one, so lifting the
cluster out cannot create a circular import.

The request model ``CreatePRFromTaskOptions`` and the ``create_pr_from_task`` handler
historically lived in ``routes/tasks.py`` and are re-exported there for backward
compatibility (``mcp_stdio/router.py`` imports them from ``..routes.tasks``).
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.error_ref import client_error
from server.services.http_verdict import honest_status
from server.services.task_branch import resolve_task_branch
from server.specpath import safe_spec_component

from ..project_store import get_projects_file
from .project_authz import require_task_access

logger = logging.getLogger(__name__)

router = APIRouter()


class CreatePRFromTaskOptions(BaseModel):
    title: str | None = None
    body: str | None = None
    draft: bool = False
    baseBranch: str | None = None
    targetRepo: str | None = None  # "owner/repo" for cross-fork PRs


@router.post("/{task_id}/worktree/create-pr")
@honest_status
async def create_pr_from_task(
    task_id: str,
    options: CreatePRFromTaskOptions = None,
    _access: dict = Depends(require_task_access("member")),
):
    """
    Push the worktree branch and create a GitHub Pull Request.
    Does NOT delete the worktree or branch after PR creation.
    """
    import subprocess

    if options is None:
        options = CreatePRFromTaskOptions()

    # Parse task_id to get spec_id
    # task_id could be "project_id:spec_id" or just "spec_id"
    if ":" in task_id:
        project_id, spec_id = task_id.split(":", 1)
        # Barrier BEFORE spec_id reaches any path expression (#1056). Path
        # joins collapse traversal silently, so validating after is too late.
        try:
            spec_id = safe_spec_component(spec_id)
        except ValueError:
            return {
                "success": False,
                "error": "Task ID must include project ID (format: project_id:spec_id)",
            }
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

    # Base branch first: resolving the task branch needs to know which branch
    # does NOT count as one.
    base_branch = options.baseBranch
    if not base_branch:
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
            base_branch = "main"

    # #1073: do NOT read the worktree's HEAD and call it the task branch. Under
    # the kubejob build backend the build runs in a separate pod and pushes; the
    # control plane's worktree is never switched off the base branch, so that
    # read yielded "main" and this endpoint asked GitHub to open main -> main.
    worktree_branch, branch_error = resolve_task_branch(
        worktree_path=worktree_path,
        project_path=project_path,
        spec_id=spec_id,
        base_branch=base_branch,
    )
    if not worktree_branch:
        return {
            "success": False,
            "error": f"Could not determine task branch: {branch_error}",
        }

    # Fetch latest base branch from remote
    try:
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass  # Non-fatal — rebase will use whatever is available

    # Stash any uncommitted changes before rebasing
    stashed = False
    try:
        stash_result = subprocess.run(
            ["git", "stash", "push", "-m", "aifactory-pre-rebase"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # "No local changes to save" means nothing was stashed
        stashed = (
            stash_result.returncode == 0
            and "No local changes" not in stash_result.stdout
        )
    except Exception:
        pass

    # Rebase onto latest base branch to minimize conflicts (best-effort)
    rebase_failed = False
    try:
        result = subprocess.run(
            ["git", "rebase", f"origin/{base_branch}"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            # Abort the failed rebase to leave worktree clean
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            rebase_failed = True
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        rebase_failed = True
    except Exception:
        rebase_failed = True

    # Restore stashed changes
    if stashed:
        subprocess.run(
            ["git", "stash", "pop"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )

    # Authenticate git via the gh credential helper before pushing (#540).
    # The deployed pod has a gh token but no git credential config, so a raw
    # `git push` over HTTPS fails with "could not read Username for
    # https://github.com". `gh auth setup-git` wires gh as the credential
    # helper — same fix as pr_endgame.create_pr / agent_service. Best-effort:
    # a failure here surfaces as the existing push error below, not a new mode.
    subprocess.run(
        ["gh", "auth", "setup-git"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=15,
    )

    # Push the branch to remote
    # Use --force-with-lease after successful rebase (rebase rewrites history)
    push_cmd = ["git", "push", "-u", "origin", worktree_branch]
    if not rebase_failed:
        push_cmd = [
            "git",
            "push",
            "--force-with-lease",
            "-u",
            "origin",
            worktree_branch,
        ]
    try:
        result = subprocess.run(
            push_cmd, cwd=worktree_path, capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"Failed to push branch: {result.stderr.strip()}",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Push timed out"}
    except Exception as e:
        return {
            "success": False,
            "error": client_error(logger, "Failed to push branch", e),
        }

    # Load task title/description for PR defaults
    pr_title = options.title
    pr_body = options.body

    if not pr_title or not pr_body:
        # Try requirements.json first
        req_file = spec_dir / "requirements.json"
        spec_file = spec_dir / "spec.md"

        if req_file.exists():
            try:
                reqs = json.loads(req_file.read_text())
                if not pr_title:
                    pr_title = reqs.get("title") or reqs.get("taskTitle") or task_id
                if not pr_body:
                    pr_body = (
                        reqs.get("description") or reqs.get("taskDescription") or ""
                    )
            except (json.JSONDecodeError, KeyError):
                pass

        if not pr_title:
            pr_title = task_id
        if not pr_body and spec_file.exists():
            try:
                pr_body = spec_file.read_text()[:2000]
            except Exception:
                pr_body = ""

    # Route PR creation through the configured git provider. When the project
    # is on GitLab or Azure DevOps the gh CLI path can't open the PR (we
    # pushed to the GitLab `origin`, not to a GitHub remote). Only fall back
    # to `gh pr create` when the project is actually a GitHub project.
    from .github import _get_project_provider, _use_provider_api, run_gh_command

    if _use_provider_api(project_id):
        try:
            provider = _get_project_provider(project_id)
            provider_type_value = getattr(
                provider.provider_type, "value", str(provider.provider_type)
            )
            if provider_type_value == "github":
                # The provider abstraction picks GitHub when a custom token is
                # configured; the gh CLI path below already handles GitHub, so
                # let it run.
                pass
            else:
                created = await provider.create_pr(
                    source_branch=worktree_branch,
                    target_branch=base_branch,
                    title=pr_title,
                    body=pr_body or "",
                    draft=bool(options.draft),
                )
                return {
                    "success": True,
                    "data": {
                        "prUrl": created.get("web_url") or "",
                        "prNumber": created.get("number"),
                        "branch": worktree_branch,
                        "baseBranch": base_branch,
                        "provider": provider_type_value,
                    },
                }
        except AttributeError:
            # Provider hasn't implemented create_pr yet — surface a clear error
            # instead of silently falling through to gh CLI (which would hit
            # the wrong remote and produce GraphQL noise).
            return {
                "success": False,
                "error": f"Provider {provider_type_value!r} does not support PR creation yet",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": client_error(logger, "Failed to create PR", exc),
            }

    # Create the PR using gh CLI (GitHub-only path)
    head_ref = worktree_branch
    gh_args = [
        "pr",
        "create",
        "--head",
        head_ref,
        "--base",
        base_branch,
        "--title",
        pr_title,
        "--body",
        pr_body or "",
    ]

    if options.targetRepo:
        # Cross-fork PR: need owner:branch format for --head
        try:
            origin_url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if origin_url_result.returncode == 0:
                import re as _re

                m = _re.search(
                    r"[:/]([^/]+)/[^/]+?(?:\.git)?$", origin_url_result.stdout.strip()
                )
                if m:
                    fork_owner = m.group(1)
                    # Update --head to owner:branch format required by gh for cross-repo PRs
                    head_idx = gh_args.index("--head") + 1
                    gh_args[head_idx] = f"{fork_owner}:{worktree_branch}"
        except Exception:
            pass  # Fall back to plain branch name
        gh_args.extend(["--repo", options.targetRepo])

    if options.draft:
        gh_args.append("--draft")

    gh_result = run_gh_command(gh_args, cwd=str(project_path))

    if not gh_result["success"]:
        return {
            "success": False,
            "error": f"Failed to create PR: {gh_result.get('error', 'unknown error')}",
        }

    # Parse PR URL from output
    pr_url = gh_result.get("output", "").strip()
    pr_number = None
    if pr_url:
        # gh pr create outputs the PR URL, extract number from it
        import re as _re

        match = _re.search(r"/pull/(\d+)", pr_url)
        if match:
            pr_number = int(match.group(1))

    return {
        "success": True,
        "data": {
            "prUrl": pr_url,
            "prNumber": pr_number,
            "branch": worktree_branch,
            "baseBranch": base_branch,
        },
    }
