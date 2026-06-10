"""PR endgame orchestrator (#71 Phase 4).

On a clean, QA-passed AIFactory build this optionally drives the finish line:

  1. push the worktree branch + open a PR            (gated by AIFACTORY_AUTO_PR)
  2. request a GitHub Copilot review on that PR
  3. watch (bounded) for Copilot's review verdict
  4. on APPROVED, merge it                          (gated by AIFACTORY_AUTO_MERGE)
  5. re-run TFactory against the merged result

Human-stop is the default safety: on CHANGES_REQUESTED, a review timeout, or a
merge conflict the PR is LEFT OPEN for a person — nothing is force-merged. Both
flags default OFF, so this module is completely inert until explicitly enabled.

Every git/gh interaction goes through an injectable ``runner`` so the whole
chain is unit-testable without git, gh, or the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Copilot's code-review reviewer slug (GitHub's automated PR reviewer). Requesting
# it is best-effort — a human can always review if the slug/rollout differs.
COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"

# How long to wait for Copilot's verdict before handing back to a human.
_POLL_INTERVAL_SECONDS = 30
_MAX_POLL_MINUTES = 20


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


Runner = Callable[[list[str], "str | None"], CmdResult]


def _default_runner(argv: list[str], cwd: str | None = None) -> CmdResult:
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    return CmdResult(p.returncode, p.stdout.strip(), p.stderr.strip())


# ---------------------------------------------------------------------------
# Flag gates — both default OFF
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}


def is_auto_pr_enabled() -> bool:
    return os.environ.get("AIFACTORY_AUTO_PR", "").strip().lower() in _TRUTHY


def is_auto_merge_enabled() -> bool:
    return os.environ.get("AIFACTORY_AUTO_MERGE", "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# GitHub primitives (all via the injectable runner)
# ---------------------------------------------------------------------------


def create_pr(
    *,
    worktree: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    runner: Runner = _default_runner,
) -> int | None:
    """Push the worktree branch and open a PR. Returns the PR number, or None.

    Idempotent-ish: if a PR for the branch already exists, gh reports it and we
    parse the number out.
    """
    # Ensure git can authenticate the push: configure gh as the credential
    # helper (idempotent, best-effort). Without this the deployed pod's raw
    # `git push` fails with "could not read Username" even though gh itself is
    # authenticated via GITHUB_TOKEN — the PR would never open.
    runner(["gh", "auth", "setup-git"], None)

    push = runner(["git", "push", "-u", "origin", branch], str(worktree))
    if not push.ok and "up-to-date" not in (push.err + push.out).lower():
        logger.warning("[pr-endgame] git push failed: %s", push.err[:300])
        # keep going — the branch may already be pushed

    res = runner(
        [
            "gh", "pr", "create", "--base", base, "--head", branch,
            "--title", title, "--body", body,
        ],
        str(worktree),
    )
    text = (res.out + "\n" + res.err)
    pr = _parse_pr_number(text)
    if pr is None and not res.ok:
        logger.warning("[pr-endgame] gh pr create failed: %s", res.err[:300])
    return pr


def _parse_pr_number(text: str) -> int | None:
    """Pull a PR number out of a gh URL like .../pull/123 (create or 'already exists')."""
    import re

    m = re.search(r"/pull/(\d+)", text or "")
    return int(m.group(1)) if m else None


def request_copilot_review(
    owner: str, repo: str, pr: int, *, runner: Runner = _default_runner
) -> bool:
    """Ask GitHub Copilot to review the PR. Best-effort (never fatal)."""
    res = runner(
        [
            "gh", "api", "--method", "POST",
            f"/repos/{owner}/{repo}/pulls/{pr}/requested_reviewers",
            "-f", f"reviewers[]={COPILOT_REVIEWER}",
        ],
        None,
    )
    if not res.ok:
        logger.info("[pr-endgame] copilot review request not accepted: %s", res.err[:200])
    return res.ok


def read_review_verdict(
    owner: str, repo: str, pr: int, *, runner: Runner = _default_runner
) -> str:
    """Return the PR's current verdict: 'approved' | 'changes_requested' | 'pending'.

    CHANGES_REQUESTED dominates (a human-stop), then APPROVED, else pending. We
    consider all reviews — a Copilot approval and a human approval both count.
    """
    res = runner(
        ["gh", "api", f"/repos/{owner}/{repo}/pulls/{pr}/reviews", "--jq",
         "[.[] | .state]"],
        None,
    )
    if not res.ok:
        return "pending"
    try:
        states = json.loads(res.out or "[]")
    except (ValueError, TypeError):
        return "pending"
    states = {str(s).upper() for s in states}
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    return "pending"


def merge_pr(
    owner: str, repo: str, pr: int, *, method: str = "squash", runner: Runner = _default_runner
) -> bool:
    """Merge the PR. Returns True on success. Never force-merges past a conflict."""
    flag = {"squash": "--squash", "rebase": "--rebase", "merge": "--merge"}.get(method, "--squash")
    res = runner(["gh", "pr", "merge", str(pr), flag, "--repo", f"{owner}/{repo}"], None)
    if not res.ok:
        logger.warning("[pr-endgame] merge failed pr=%d: %s", pr, res.err[:300])
    return res.ok


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _split_repo(repo: str) -> tuple[str, str] | None:
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        if owner and name:
            return owner, name
    return None


async def watch_and_finish(
    *,
    owner: str,
    repo: str,
    pr: int,
    on_approved_merged: Callable[[], None] | None = None,
    runner: Runner = _default_runner,
    poll_interval: int = _POLL_INTERVAL_SECONDS,
    max_minutes: int = _MAX_POLL_MINUTES,
) -> dict:
    """Poll the PR verdict; on APPROVED (+ AIFACTORY_AUTO_MERGE) merge it.

    Returns a result dict. Never raises. Leaves the PR open for a human on
    changes_requested / timeout / merge failure.
    """
    polls = max(1, (max_minutes * 60) // max(1, poll_interval))
    for _ in range(polls):
        await asyncio.sleep(poll_interval)
        verdict = await asyncio.to_thread(
            read_review_verdict, owner, repo, pr, runner=runner
        )
        if verdict == "changes_requested":
            logger.info("[pr-endgame] pr=%d changes requested — handing to a human", pr)
            return {"pr": pr, "verdict": verdict, "merged": False, "reason": "changes_requested"}
        if verdict == "approved":
            if not is_auto_merge_enabled():
                return {"pr": pr, "verdict": verdict, "merged": False, "reason": "auto_merge_disabled"}
            merged = await asyncio.to_thread(merge_pr, owner, repo, pr, runner=runner)
            if merged and on_approved_merged is not None:
                try:
                    on_approved_merged()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[pr-endgame] post-merge re-test hook failed: %s", exc)
            return {"pr": pr, "verdict": verdict, "merged": merged,
                    "reason": None if merged else "merge_failed"}
    return {"pr": pr, "verdict": "pending", "merged": False, "reason": "review_timeout"}


def _pr_title_body(spec_dir: Path, spec_id: str) -> tuple[str, str]:
    title = spec_id
    body = "Automated PR from AIFactory (clean build)."
    try:
        req = json.loads((spec_dir / "requirements.json").read_text())
        title = str(req.get("title") or spec_id)[:120]
        desc = req.get("description")
        if desc:
            body = f"{desc}\n\n---\n🤖 AIFactory auto-PR on a clean, QA-passed build."
    except (OSError, ValueError):
        pass
    return title, body


def gather_pr_context(
    project_path: Path, spec_dir: Path, spec_id: str, *, runner: Runner = _default_runner
) -> dict | None:
    """Resolve {worktree, branch, base, repo} for a finished task, or None.

    Returns None (skip the endgame) when there's no worktree branch or no
    resolvable GitHub repo — both required to open a PR.
    """
    worktree = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    if not worktree.exists():
        return None
    head = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], str(worktree))
    branch = head.out.strip() if head.ok else ""
    if not branch or branch == "HEAD":
        return None

    repo = ""
    base = "main"
    try:
        req = json.loads((spec_dir / "requirements.json").read_text())
        gh = req.get("githubIssue") if isinstance(req, dict) else None
        if isinstance(gh, dict):
            repo = gh.get("repo") or gh.get("repository") or ""
        repo = repo or req.get("github_repo") or req.get("repo") or ""
    except (OSError, ValueError):
        pass
    if not repo:
        try:
            meta = json.loads((spec_dir / "task_metadata.json").read_text())
            repo = meta.get("github_repo") or meta.get("githubRepo") or ""
            base = meta.get("base_branch") or meta.get("baseBranch") or base
        except (OSError, ValueError):
            pass
    if not repo:
        # Last resort: derive owner/name from the worktree's origin remote.
        url = runner(["git", "remote", "get-url", "origin"], str(worktree))
        if url.ok:
            import re

            m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url.out.strip())
            if m:
                repo = m.group(1)
    if not repo:
        return None
    return {"worktree": worktree, "branch": branch, "base": base, "repo": repo}


async def run_pr_endgame(
    *,
    spec_dir: Path,
    spec_id: str,
    worktree: Path,
    branch: str,
    base: str,
    repo: str,
    re_test: Callable[[], None] | None = None,
    runner: Runner = _default_runner,
    background: bool = True,
) -> dict:
    """Drive the endgame: open a PR, request Copilot review, then (in the
    background) wait for the verdict and merge+re-test on approval.

    Caller guards with ``is_auto_pr_enabled()``. Returns immediately with the PR
    number once it's opened; the verdict-watch runs as a background task unless
    ``background=False`` (tests await it inline). Never raises.
    """
    parts = _split_repo(repo)
    if parts is None:
        return {"ok": False, "reason": "no_repo", "repo": repo}
    owner, name = parts

    title, body = _pr_title_body(spec_dir, spec_id)
    try:
        pr = await asyncio.to_thread(
            lambda: create_pr(worktree=worktree, branch=branch, base=base,
                              title=title, body=body, runner=runner)
        )
    except Exception as exc:  # noqa: BLE001 — endgame must never break completion
        logger.warning("[pr-endgame] create_pr error: %s", exc)
        return {"ok": False, "reason": "create_pr_error", "error": str(exc)[:300]}
    if pr is None:
        return {"ok": False, "reason": "pr_not_created"}

    await asyncio.to_thread(request_copilot_review, owner, name, pr, runner=runner)
    logger.info("[pr-endgame] opened PR #%d for %s; requested Copilot review", pr, spec_id)

    coro = watch_and_finish(
        owner=owner, repo=name, pr=pr, on_approved_merged=re_test, runner=runner
    )
    if background:
        asyncio.create_task(coro)
        return {"ok": True, "pr": pr, "watching": True}
    result = await coro
    return {"ok": True, "pr": pr, "watching": False, **result}
