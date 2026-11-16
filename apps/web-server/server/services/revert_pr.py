"""Open a revert PR after a post-merge regression on a commit the pipeline merged (#2054).

The PR is never merged here: no code path in this module calls ``gh pr merge``.
The merge record lives in the server data dir, not in the project spec dir,
because project members can write ``.aifactory`` files.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log

from .pr_endgame import Runner, _default_runner

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SHA = re.compile(r"[0-9a-f]{7,40}")
_MUTATION = (
    "mutation($id:ID!,$title:String,$body:String){"
    "revertPullRequest(input:{pullRequestId:$id,title:$title,body:$body})"
    "{revertPullRequest{number url}}}"
)


def _record_path(merge_sha: str) -> Path:
    from server.paths import get_data_dir  # noqa: PLC0415 - migrates on import

    d = get_data_dir() / "pipeline_merges"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{merge_sha}.json"


def _view(owner: str, repo: str, pr: int, runner: Runner) -> dict[str, Any] | None:
    res = runner(
        [
            "gh", "pr", "view", str(pr), "--repo", f"{owner}/{repo}", "--json",
            "id,state,title,headRefName,mergeCommit",
        ],
        None,
    )  # fmt: skip
    if not res.ok:
        return None
    try:
        doc = json.loads(res.out)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def record_pipeline_merge(  # noqa: PLR0913 - one record, all fields required
    project_id: str,
    spec_id: str,
    owner: str,
    repo: str,
    pr: int,
    branch: str,
    runner: Runner = _default_runner,
) -> bool:
    """Remember that the pipeline merged ``pr``. Best-effort, never raises."""
    try:
        doc = _view(owner, repo, pr, runner) or {}
        sha = str((doc.get("mergeCommit") or {}).get("oid") or "")
        if not _SHA.fullmatch(sha):
            return False
        _record_path(sha).write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "spec_id": spec_id,
                    "owner": owner,
                    "repo": repo,
                    "pr": pr,
                    "branch": branch,
                    "merge_sha": sha,
                }
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001 - must never skip the re-test
        logger.warning("[revert-pr] merge record skipped: %s", sanitize_log(exc))
        return False


def _find_record(failing_sha: str) -> tuple[Path, dict[str, Any]] | None:
    """The record whose merge sha is ``failing_sha`` (full, or a prefix of >= 7)."""
    sha = failing_sha.strip().lower()
    if not _SHA.fullmatch(sha):
        return None
    for path in sorted(_record_path("x").parent.glob(f"{sha}*.json")):
        try:
            return path, json.loads(path.read_text())
        except (OSError, ValueError):
            return None
    return None


def _existing_revert(owner: str, repo: str, pr: int, runner: Runner) -> str | None:
    """A PR of any state whose body says it reverts ``pr``."""
    phrase = f"Reverts {owner}/{repo}#{pr}"
    res = runner(
        [
            "gh", "pr", "list", "--repo", f"{owner}/{repo}", "--state", "all",
            "--limit", "100", "--search", f'"{phrase}" in:body', "--json",
            "url,body",
        ],
        None,
    )  # fmt: skip
    if not res.ok:
        return None
    try:
        rows = json.loads(res.out)
    except ValueError:
        return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and phrase in str(row.get("body") or ""):
            return str(row.get("url") or "") or None
    return None


def open_revert_pr(  # noqa: PLR0913 - the caller names every input
    *,
    project_id: str,
    spec_id: str,
    failing_sha: str,
    run_url: str,
    transcript_uris: list[str],
    runner: Runner = _default_runner,
) -> dict[str, Any]:
    """Open (once) a revert PR for the pipeline-merged commit ``failing_sha``."""
    with _LOCK:
        found = _find_record(failing_sha)
        if found is None:
            return {"ok": False, "reason": "not_pipeline_merged"}
        path, rec = found
        if rec.get("project_id") != project_id or rec.get("spec_id") != spec_id:
            return {"ok": False, "reason": "not_pipeline_merged"}
        if rec.get("revert_pr_url"):
            return {"ok": True, "reason": "exists", "url": rec["revert_pr_url"]}
        owner, repo, pr = rec["owner"], rec["repo"], int(rec["pr"])
        view = _view(owner, repo, pr, runner)
        if (
            not view
            or view.get("state") != "MERGED"
            or view.get("headRefName") != rec.get("branch")
            or (view.get("mergeCommit") or {}).get("oid") != rec.get("merge_sha")
        ):
            return {"ok": False, "reason": "merge_not_confirmed"}
        url = _existing_revert(owner, repo, pr, runner)
        reason = "exists"
        if url is None:
            uris = "\n".join(f"- `{u}`" for u in transcript_uris) or "- none recorded"
            body = (
                f"Reverts {owner}/{repo}#{pr}\n\n"
                f"A post-merge regression run failed on `{rec['merge_sha'][:12]}`:\n"
                f"{run_url}\n\n"
                "Agent transcripts recorded for this task (the key has no run id "
                f"yet, so one may belong to an earlier run):\n{uris}\n\n"
                "Opened automatically. It is never merged automatically: a person "
                "must review and approve it."
            )
            res = runner(
                [
                    "gh", "api", "graphql", "-f", f"query={_MUTATION}", "-f",
                    f"id={view['id']}", "-f", f'title=Revert "{view["title"]}"',
                    "-f", f"body={body}",
                ],
                None,
            )  # fmt: skip
            try:
                created = json.loads(res.out)["data"]["revertPullRequest"][
                    "revertPullRequest"
                ]
                url = str(created["url"])
                reason = "created"
            except (ValueError, KeyError, TypeError):
                return {"ok": False, "reason": "revert_failed"}
        rec["revert_pr_url"] = url
        try:
            path.write_text(json.dumps(rec))
        except OSError:
            logger.warning("[revert-pr] could not store the revert URL")
        return {"ok": True, "reason": reason, "url": url}
