"""Recording an approval, and merging the pull request it approves (#1076).

Extracted from routes/worktree_merge.py, which is over 2000 lines. Beyond the
size, the extraction has a concrete benefit: adding ~95 lines near the top of
that file shifted every line below it, and CodeQL re-reported a dozen
PRE-EXISTING alerts as new because their line numbers moved. Code that does not
belong in a route module was making an unrelated security gate noisy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from server.services.gh import run_gh_command
from server.services.task_status import write_status
from server.specpath import safe_spec_component


def approved(
    project_path: Path, spec_id: str, message: str, **extra: object
) -> dict[str, Any]:
    """Record the approval and shape the success response.

    Shared by the PR-merge and local-merge paths so they cannot disagree about
    what "merged" writes -- which is exactly how one of them ends up not
    writing it at all (#1071).

    spec_id, NOT task_id: task_id still carries the "project_id:" prefix, so
    joining it addresses a directory that does not exist -- and write_control
    would helpfully create it, landing the status nowhere.
    """
    # Imported and bound locally, exactly as the six sibling functions do.
    # Referencing a module-level logger raised NameError on the status-error
    # path -- the code reporting a failure would itself have failed, the same
    # defect #649 fixed in merge_worktree. Hoisting the import to module scope
    # instead would turn all six of those local imports into repeated imports,
    # so the convention is kept rather than half-changed.
    import logging  # noqa: PLC0415 - module convention, see above

    logger = logging.getLogger(__name__)

    # Barriered HERE, not just at the call sites. Callers do sanitise, but a
    # helper that trusts its parameter is one refactor away from a caller that
    # does not -- and the analyser is right to say so. safe_spec_component is
    # idempotent, so an already-safe value passes straight through.
    status_error = write_status(
        project_path / ".aifactory" / "specs" / safe_spec_component(spec_id),
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


def _open_pr(project_path: Path, branch: str) -> tuple[int, str] | None:
    """The most recent PR for *branch* as (number, state), or None if there is none."""
    found = run_gh_command(
        [
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            "number,state",
        ],
        cwd=str(project_path),
    )
    if not found.get("success"):
        return None
    try:
        prs = json.loads(found.get("output") or "[]")
    except ValueError:
        return None
    if not prs:
        return None
    return prs[0].get("number"), (prs[0].get("state") or "").upper()


def merge_pull_request(project_path: Path, branch: str) -> tuple[bool, str]:
    """Merge the open PR for *branch*. Returns ``(merged, detail)``.

    ``(True, detail)``  -- merged now, or ALREADY merged. Idempotent on
                           purpose: Approve must not fail because someone
                           merged it first, or because it is clicked twice.
    ``(False, detail)`` -- a PR exists and could not be merged; detail says why.
    ``(False, "")``     -- no PR for this branch; the caller may fall back.
    """
    found = _open_pr(project_path, branch)
    if found is None:
        return False, ""
    number, state = found

    if state == "MERGED":
        return True, f"pull request #{number} was already merged"
    if state == "CLOSED":
        return False, f"pull request #{number} is closed; reopen it to merge"

    merged = run_gh_command(
        ["pr", "merge", str(number), "--squash"], cwd=str(project_path)
    )
    if merged.get("success"):
        return True, f"merged pull request #{number}"
    # The gh stderr is LOGGED, not returned: it can carry command lines, paths
    # and token-bearing URLs, and this string is rendered in the cockpit. The
    # caller still learns which PR failed and where to look.
    import logging  # noqa: PLC0415 - module convention, see _approved

    logging.getLogger(__name__).warning(
        "gh pr merge failed for #%s: %s", number, merged.get("error")
    )
    return False, (
        f"could not merge pull request #{number}; GitHub refused it "
        f"(check its conflicts, required checks and branch protection)"
    )
