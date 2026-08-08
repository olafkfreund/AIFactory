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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.services.task_branch import resolve_task_branch

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
_FALSY = {"0", "false", "no", "off"}


def _project_env(project_path: Path | None, key: str) -> str | None:
    """Read ``key`` from a project's ``.aifactory/.env`` (the per-project settings
    store the Settings UI writes via PATCH /api/projects/{id}/settings). Returns
    None when unset/unreadable so the caller can fall back to the global env.
    """
    if project_path is None:
        return None
    env_path = Path(project_path) / ".aifactory" / ".env"
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except OSError:
        return None
    return None


def _flag(key: str, project_path: Path | None) -> bool:
    """Resolve a boolean flag: per-project setting (Settings UI) wins, else the
    global env var (deployment default). Both default OFF."""
    pv = _project_env(project_path, key)
    if pv is not None:
        return pv.strip().lower() in _TRUTHY
    return os.environ.get(key, "").strip().lower() in _TRUTHY


def is_auto_pr_enabled(project_path: Path | None = None) -> bool:
    return _flag("AIFACTORY_AUTO_PR", project_path)


def is_auto_merge_enabled(project_path: Path | None = None) -> bool:
    return _flag("AIFACTORY_AUTO_MERGE", project_path)


def read_task_metadata(spec_dir: Path | None) -> dict[str, Any]:
    """A task's ``task_metadata.json`` as a dict, or ``{}``.

    THE reader for that file in this module -- ``gather_pr_context`` used to
    inline its own copy. One reader means one place where the path is built and
    one place where a malformed file is tolerated.

    Never raises: an unreadable or non-object file is indistinguishable from an
    absent one to every caller here, and none of them may fail a build over it.
    """
    if spec_dir is None:
        return {}
    try:
        meta = json.loads((spec_dir / "task_metadata.json").read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def read_review_tier(spec_dir: Path | None) -> str | None:
    """The RFC-0011 ``reviewTier`` a task was routed with, or None (#1158).

    Written by ``intake.build_execution_block`` (low -> auto, medium -> async,
    hard -> blocking) and carried into ``task_metadata.json`` by
    ``trusted_plan._EXECUTION_TO_METADATA``. Until now nothing read it back --
    it was contract surface that read as implemented.

    None on any absence/unreadability, which the caller treats as "no opinion".
    """
    tier = read_task_metadata(spec_dir).get("reviewTier")
    return tier if isinstance(tier, str) and tier.strip() else None


# The only tier spellings that may ever appear in a log line. Anything else is
# reported as the constant below rather than echoed, so a value crafted into
# task_metadata.json cannot forge log entries (py/log-injection).
_LOGGABLE_TIERS = frozenset({"auto", "async", "blocking", "low", "medium", "hard"})


def _describe_tier(tier: str | None) -> str:
    if tier is None:
        return "none"
    normalised = tier.strip().lower()
    return normalised if normalised in _LOGGABLE_TIERS else "unrecognised"


def tier_allows_auto_merge(spec_dir: Path | None) -> bool:
    """Whether this task's review tier permits auto-merge at all (#1158).

    Delegates the tier policy to ``merge.merge_policy`` -- the RFC-0011 routing
    table lives there and must not be re-implemented here. Lazy import: the
    backend package is on sys.path only at runtime (same pattern as
    ``conflict_service``), and a missing backend must not break the endgame, so
    an import failure degrades to "no opinion" rather than blocking a merge the
    operator asked for.
    """
    tier = read_review_tier(spec_dir)
    if tier is None:
        return True
    try:
        from merge.merge_policy import tier_permits_auto_merge  # noqa: PLC0415
    except ImportError:  # pragma: no cover - backend always present in prod
        logger.warning(
            "[pr-endgame] merge_policy unavailable; reviewTier=%s not applied", tier
        )
        return True
    return tier_permits_auto_merge(tier)


# Which reviewer gates the merge. "aifactory" = AIFactory's own review engine
# (Claude/Ollama — no Copilot credits, gated on the engine's verdict since GitHub
# forbids self-approving the PR we opened). "copilot" = GitHub Copilot's review.
# "any" = any APPROVED GitHub review. Default "aifactory".
_VALID_REVIEWERS = {"aifactory", "copilot", "any"}


def resolve_pr_reviewer(project_path: Path | None = None) -> str:
    pv = _project_env(project_path, "AIFACTORY_PR_REVIEWER")
    val = (
        (pv if pv is not None else os.environ.get("AIFACTORY_PR_REVIEWER", ""))
        .strip()
        .lower()
    )
    return val if val in _VALID_REVIEWERS else "aifactory"


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

    # #959: on the kubejob/packed path the build ran in a k8s Job on an ephemeral
    # /work emptyDir and pushed its branch to origin from there
    # (core.workspace_fetch.maybe_push_workspace_branch, #751). THIS control-plane
    # worktree stayed on the base branch and has NO local ref for that branch, so
    # the push below fails "src refspec <branch> does not match any" and gh pr
    # create is left without a present head → no PR opens. Fetch the branch from
    # origin into a local ref first so both the push and gh pr create operate on a
    # branch that exists locally. Fail-safe: on the co-mount path (branch already
    # local) or when origin has no such ref, the fetch is a harmless no-op and we
    # fall through to the existing behaviour. `branch:branch` (non-force) creates
    # the local ref on the packed path where none exists; a divergent local ref
    # just declines the fast-forward, which is fine.
    fetch = runner(["git", "fetch", "origin", f"{branch}:{branch}"], str(worktree))
    if not fetch.ok:
        logger.info(
            "[pr-endgame] fetch of %s from origin skipped (%s) — branch may be "
            "local already or not yet on origin",
            branch,
            (fetch.err or fetch.out)[:200],
        )

    push = runner(["git", "push", "-u", "origin", branch], str(worktree))
    if not push.ok and "up-to-date" not in (push.err + push.out).lower():
        logger.warning("[pr-endgame] git push failed: %s", push.err[:300])
        # keep going — the branch may already be pushed

    res = runner(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        str(worktree),
    )
    text = res.out + "\n" + res.err
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
            "gh",
            "api",
            "--method",
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr}/requested_reviewers",
            "-f",
            f"reviewers[]={COPILOT_REVIEWER}",
        ],
        None,
    )
    if not res.ok:
        logger.info(
            "[pr-endgame] copilot review request not accepted: %s", res.err[:200]
        )
    return res.ok


@dataclass
class ReviewState:
    """Copilot-aware view of a PR's reviews."""

    verdict: str  # 'approved' | 'changes_requested' | 'pending'
    copilot_reviewed: bool = False
    copilot_approved: bool = False
    copilot_changes_requested: bool = False
    findings: list = field(default_factory=list)  # for the auto-feedback loop


def _is_copilot(login: str) -> bool:
    return "copilot" in (login or "").lower()


# Severities that block a merge when AIFactory's own engine is the reviewer.
_BLOCKING_SEVERITIES = {"critical", "high", "blocker", "blocking", "error"}


def verdict_from_review_result(result: dict | None) -> ReviewState:
    """Map AIFactory's stored PR-review result to a ReviewState.

    Used when the reviewer is ``aifactory`` (GitHub forbids self-approving the
    PR we opened, so we gate on the engine's verdict, not a GitHub review event).
    A stored ``verdict`` wins; otherwise we derive it from finding severities —
    any blocking/critical/high finding ⇒ changes_requested, else approved. A
    missing/empty result ⇒ pending (review not done yet).
    """
    if not isinstance(result, dict) or not result:
        return ReviewState("pending")
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    raw = str(data.get("verdict") or data.get("recommendation") or "").lower()
    # AIFactory's engine MergeVerdict vocabulary (models.MergeVerdict) + generic
    # synonyms. ready_to_merge == approved; everything else that's a real verdict
    # is changes_requested (the auto-feedback loop will try to resolve it).
    _APPROVE = {"approve", "approved", "ready_to_merge"}
    _CHANGES = {
        "request_changes",
        "changes_requested",
        "reject",
        "rejected",
        "merge_with_changes",
        "needs_revision",
        "blocked",
        "do_not_merge",
    }
    blockers = data.get("blockers")
    if isinstance(blockers, list) and blockers:
        return ReviewState("changes_requested", findings=blockers)
    if raw in _APPROVE:
        return ReviewState("approved")
    if raw in _CHANGES:
        return ReviewState("changes_requested", findings=(data.get("findings") or []))
    findings = data.get("findings")
    if isinstance(findings, list):
        blocking = [
            f
            for f in findings
            if isinstance(f, dict)
            and str(f.get("severity", "")).lower() in _BLOCKING_SEVERITIES
        ]
        if blocking:
            return ReviewState("changes_requested", findings=blocking)
        return ReviewState("approved")
    return ReviewState("pending")


def read_review_verdict(
    owner: str, repo: str, pr: int, *, runner: Runner = _default_runner
) -> ReviewState:
    """Summarize a PR's reviews with Copilot called out explicitly.

    ``verdict`` is the overall latest-per-reviewer signal (CHANGES_REQUESTED
    dominates APPROVED). The ``copilot_*`` fields let the watcher REQUIRE that
    GitHub Copilot's code review actually ran and approved before merging — we
    want to use Copilot's findings, not merge around them.
    """
    res = runner(
        [
            "gh",
            "api",
            f"/repos/{owner}/{repo}/pulls/{pr}/reviews",
            "--jq",
            "[.[] | {state, login: .user.login}]",
        ],
        None,
    )
    if not res.ok:
        return ReviewState("pending")
    try:
        reviews = json.loads(res.out or "[]")
    except (ValueError, TypeError):
        return ReviewState("pending")

    states = {str(r.get("state", "")).upper() for r in reviews if isinstance(r, dict)}
    copilot_states = {
        str(r.get("state", "")).upper()
        for r in reviews
        if isinstance(r, dict) and _is_copilot(r.get("login", ""))
    }
    verdict = (
        "changes_requested"
        if "CHANGES_REQUESTED" in states
        else "approved"
        if "APPROVED" in states
        else "pending"
    )
    return ReviewState(
        verdict=verdict,
        copilot_reviewed=bool(copilot_states),
        copilot_approved="APPROVED" in copilot_states,
        copilot_changes_requested="CHANGES_REQUESTED" in copilot_states,
    )


def merge_pr(
    owner: str,
    repo: str,
    pr: int,
    *,
    method: str = "squash",
    runner: Runner = _default_runner,
) -> bool:
    """Merge the PR. Returns True on success. Never force-merges past a conflict.

    On a non-clean merge (branch behind base — the common sequential case where
    an earlier auto-merge advanced main), update the PR branch from base once and
    retry. A TRUE line-level conflict (update-branch fails) is left for a human —
    we never force it.
    """
    flag = {"squash": "--squash", "rebase": "--rebase", "merge": "--merge"}.get(
        method, "--squash"
    )
    full = f"{owner}/{repo}"
    res = runner(["gh", "pr", "merge", str(pr), flag, "--repo", full], None)
    if res.ok:
        return True

    blob = (res.err + " " + res.out).lower()
    if any(
        s in blob for s in ("not mergeable", "cannot be cleanly", "conflict", "behind")
    ):
        logger.info(
            "[pr-endgame] pr=%d not cleanly mergeable — updating branch from base", pr
        )
        upd = runner(["gh", "pr", "update-branch", str(pr), "--repo", full], None)
        if upd.ok:
            res2 = runner(["gh", "pr", "merge", str(pr), flag, "--repo", full], None)
            if res2.ok:
                return True
            logger.warning(
                "[pr-endgame] merge retry failed pr=%d: %s", pr, res2.err[:300]
            )
            return False
        logger.info(
            "[pr-endgame] pr=%d has a true conflict update-branch can't resolve — human-stop",
            pr,
        )
        return False
    logger.warning("[pr-endgame] merge failed pr=%d: %s", pr, res.err[:300])
    return False


# ---------------------------------------------------------------------------
# True-conflict resolution (#543)
# ---------------------------------------------------------------------------


@dataclass
class ConflictResolution:
    """Outcome of one attempt to rebase a PR branch onto base + resolve conflicts."""

    resolved: bool
    conflicted_files: list[str]
    reason: str = ""


# A fixer takes the list of conflict-marked files + the worktree path and returns
# True once it has resolved the markers in-place (ready to `git add`).
ConflictFixer = Callable[["list[str]", str], bool]


def resolve_pr_conflicts(
    worktree: str,
    base_branch: str,
    *,
    fixer: ConflictFixer,
    runner: Runner = _default_runner,
) -> ConflictResolution:
    """Rebase the PR branch onto ``base_branch`` in ``worktree`` and, on a true
    line-level conflict, hand the conflicted files to ``fixer`` to resolve, then
    continue the rebase (#543).

    Safe-by-construction: every git step that fails leaves the rebase ABORTED
    (worktree clean), so a caller can always fall back to human-stop. This helper
    does NOT push and does NOT bound retries or re-review — the orchestrator owns
    those (push the resolved branch, re-review, cap attempts). Pure + injectable
    (``runner``/``fixer``) so it is unit-testable without real git or an LLM.
    """
    fetch = runner(["git", "fetch", "origin", base_branch], worktree)
    if not fetch.ok:
        return ConflictResolution(False, [], f"fetch failed: {fetch.err[:200]}")

    base_ref = f"origin/{base_branch}"
    reb = runner(["git", "rebase", base_ref], worktree)
    if reb.ok:
        return ConflictResolution(True, [], "rebase clean (no conflict)")

    status = runner(["git", "diff", "--name-only", "--diff-filter=U"], worktree)
    conflicted = [f for f in status.out.splitlines() if f.strip()]
    if not conflicted:
        # Rebase failed for some reason other than mergeable conflicts.
        runner(["git", "rebase", "--abort"], worktree)
        return ConflictResolution(
            False, [], f"rebase failed with no conflicted files: {reb.err[:200]}"
        )

    if not fixer(conflicted, worktree):
        runner(["git", "rebase", "--abort"], worktree)
        return ConflictResolution(
            False, conflicted, "fixer did not resolve the conflicts"
        )

    runner(["git", "add", "-A"], worktree)
    # core.editor=true: non-interactive `rebase --continue` (no $EDITOR prompt).
    cont = runner(["git", "-c", "core.editor=true", "rebase", "--continue"], worktree)
    if not cont.ok:
        runner(["git", "rebase", "--abort"], worktree)
        return ConflictResolution(
            False, conflicted, f"rebase --continue failed: {cont.err[:200]}"
        )
    return ConflictResolution(True, conflicted, "resolved via fixer")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


# RFC-0020 3.5. Imported lazily so this module keeps working when it is loaded
# without the backend path shim (it is reachable from both the web server and the
# agent service), falling back to "treat it as GitHub" — which is exactly what an
# unqualified reference means anyway.
def _parse_repo_ref(repo: str) -> tuple[str, str]:
    """``(provider, bare project path)`` for a possibly-qualified reference."""
    try:
        from repo_ref import parse_repo_ref
    except ImportError:  # pragma: no cover — backend seam absent
        return "github", repo
    return parse_repo_ref(repo) or ("github", repo)


def _is_github(provider: str) -> bool:
    return (provider or "github").strip().lower() == "github"


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
    auto_merge: bool,
    require_copilot: bool = True,
    review_fn: Callable[[], ReviewState] | None = None,
    fix_fn: Callable[[list], bool] | None = None,
    max_fix_cycles: int = 2,
    on_approved_merged: Callable[[], None] | None = None,
    conflict_fixer: ConflictFixer | None = None,
    worktree: str | None = None,
    base_branch: str = "main",
    max_conflict_cycles: int = 1,
    runner: Runner = _default_runner,
    poll_interval: int = _POLL_INTERVAL_SECONDS,
    max_minutes: int = _MAX_POLL_MINUTES,
) -> dict:
    """Poll the PR's reviews and merge only on a CLEAN Copilot approval.

    Merge requires ALL of: ``auto_merge`` on, no CHANGES_REQUESTED, and — when
    ``require_copilot`` (default) — that GitHub Copilot's code review actually ran
    and APPROVED. Copilot finds real problems, so we gate on its verdict rather
    than merging around it: ``changes_requested`` (from anyone, incl. Copilot) is
    a human-stop, and if Copilot never reviews we time out to a human-stop too —
    never a blind merge. Never raises; leaves the PR open on any non-clean path.
    """
    # review_fn (AIFactory's own engine verdict) takes precedence over reading
    # GitHub review state — AIFactory can't submit a GitHub approval on a PR it
    # opened (self-approval), so its verdict is read from the engine directly.
    # With review_fn the Copilot-login gate doesn't apply (verdict is the gate).
    if review_fn is not None:
        require_copilot = False

    fix_cycles = 0
    conflict_cycles = 0
    polls = max(1, (max_minutes * 60) // max(1, poll_interval))
    for _ in range(polls):
        await asyncio.sleep(poll_interval)
        if review_fn is not None:
            rs = await asyncio.to_thread(review_fn)
        else:
            rs = await asyncio.to_thread(
                read_review_verdict, owner, repo, pr, runner=runner
            )

        if rs.verdict == "changes_requested":
            who = "copilot" if rs.copilot_changes_requested else "reviewer"
            # Auto-feedback loop (#71 Phase B): route the findings back to the
            # QA-fixer, push the fix, and re-review — bounded. Only when a fix_fn
            # is wired (aifactory reviewer); otherwise human-stop.
            if fix_fn is not None and fix_cycles < max_fix_cycles:
                fix_cycles += 1
                logger.info(
                    "[pr-endgame] pr=%d changes requested by %s — auto-fix cycle %d/%d",
                    pr,
                    who,
                    fix_cycles,
                    max_fix_cycles,
                )
                ok = await asyncio.to_thread(fix_fn, rs.findings)
                if not ok:
                    return {
                        "pr": pr,
                        "verdict": rs.verdict,
                        "merged": False,
                        "reason": "fix_failed",
                        "fix_cycles": fix_cycles,
                    }
                continue  # re-review on the next poll after the fix lands
            logger.info(
                "[pr-endgame] pr=%d changes requested by %s — handing to a human",
                pr,
                who,
            )
            return {
                "pr": pr,
                "verdict": rs.verdict,
                "merged": False,
                "reason": "changes_requested"
                if fix_cycles == 0
                else "needs_human_after_fixes",
                "fix_cycles": fix_cycles,
                "copilot_changes_requested": rs.copilot_changes_requested,
            }

        # Only consider merging on an approval that satisfies the Copilot gate.
        approved = (
            rs.copilot_approved if require_copilot else (rs.verdict == "approved")
        )
        if approved:
            if not auto_merge:
                return {
                    "pr": pr,
                    "verdict": "approved",
                    "merged": False,
                    "reason": "auto_merge_disabled",
                    "copilot_approved": rs.copilot_approved,
                }
            merged = await asyncio.to_thread(merge_pr, owner, repo, pr, runner=runner)
            # #543: an approved PR that won't merge is usually behind a true
            # line-level conflict. When a conflict_fixer is wired, rebase onto
            # base + resolve the conflicts, push, and RE-REVIEW (loop continues →
            # next poll re-reads the verdict and only merges on a fresh approve).
            # Bounded; falls through to human-stop if unresolved or push fails.
            if (
                not merged
                and conflict_fixer is not None
                and worktree
                and conflict_cycles < max_conflict_cycles
            ):
                conflict_cycles += 1
                logger.info(
                    "[pr-endgame] pr=%d approved but not mergeable — conflict "
                    "resolution cycle %d/%d",
                    pr,
                    conflict_cycles,
                    max_conflict_cycles,
                )
                cr = await asyncio.to_thread(
                    resolve_pr_conflicts,
                    worktree,
                    base_branch,
                    fixer=conflict_fixer,
                    runner=runner,
                )
                if cr.resolved:
                    push = await asyncio.to_thread(
                        runner,
                        ["git", "push", "--force-with-lease", "origin", "HEAD"],
                        worktree,
                    )
                    if push.ok:
                        continue  # re-review the rebased+resolved branch next poll
                    logger.warning(
                        "[pr-endgame] pr=%d post-resolve push failed: %s",
                        pr,
                        push.err[:200],
                    )
                logger.info(
                    "[pr-endgame] pr=%d conflict not auto-resolved (%s) — human-stop",
                    pr,
                    cr.reason,
                )
                return {
                    "pr": pr,
                    "verdict": "approved",
                    "merged": False,
                    "reason": "merge_conflict_unresolved",
                    "conflict_cycles": conflict_cycles,
                }
            if merged and on_approved_merged is not None:
                try:
                    on_approved_merged()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[pr-endgame] post-merge re-test hook failed: %s", exc
                    )
            return {
                "pr": pr,
                "verdict": "approved",
                "merged": merged,
                "reason": None if merged else "merge_failed",
                "copilot_approved": rs.copilot_approved,
            }
        # else: pending (or approved-but-not-by-Copilot when require_copilot) → keep waiting

    return {
        "pr": pr,
        "verdict": "pending",
        "merged": False,
        "reason": "review_timeout (no Copilot approval)"
        if require_copilot
        else "review_timeout",
    }


def _pr_title_body(spec_dir: Path, spec_id: str) -> tuple[str, str]:
    title = spec_id
    body = "Automated PR from AIFactory (clean build)."
    issue_number = None
    try:
        req = json.loads((spec_dir / "requirements.json").read_text())
        title = str(req.get("title") or spec_id)[:120]
        desc = req.get("description")
        if desc:
            body = f"{desc}\n\n---\n🤖 AIFactory auto-PR on a clean, QA-passed build."
        # Origin-issue correlation: from_issue stamps the number on both
        # provenance.issue_number and githubIssue.number.
        prov = req.get("provenance")
        gh = req.get("githubIssue")
        if isinstance(prov, dict) and isinstance(prov.get("issue_number"), int):
            issue_number = prov["issue_number"]
        elif isinstance(gh, dict) and isinstance(gh.get("number"), int):
            issue_number = gh["number"]
    except (OSError, ValueError):
        pass
    if issue_number is not None:
        # Closing keyword so the origin issue links to (and, on a default-branch
        # merge, closes with) the PR — the whole point of label-driven intake.
        body = f"{body}\n\nFixes #{issue_number}"
    return title, body


def gather_pr_context(
    project_path: Path,
    spec_dir: Path,
    spec_id: str,
    *,
    runner: Runner = _default_runner,
) -> dict | None:
    """Resolve {worktree, branch, base, repo, provider} for a task, or None.

    Returns None (skip the endgame) when there's no worktree branch or no
    resolvable repo — both required to open a PR.

    ``provider`` is read off the repo reference's RFC-0020 3.5 qualification
    (``gitlab:group/project``), defaulting to ``github`` for an unqualified one.
    ``repo`` is the BARE path with any qualification stripped, because that is
    what ``gh`` and ``_split_repo`` take. The caller uses ``provider`` to decide
    whether this GitHub-shaped endgame may run at all — see ``run_pr_endgame``.
    """
    worktree = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    if not worktree.exists():
        return None
    head = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], str(worktree))
    branch = head.out.strip() if head.ok else ""

    # The PR base is the task's integration branch (task_metadata.base_branch,
    # stamped by from_issue / trusted-plan ingest), defaulting to main. Read it
    # UNCONDITIONALLY — it used to live inside the repo fallback below, so any
    # spec whose requirements.json named the repo silently PR'd against main
    # even when the repo integrates via dev. It is also read BEFORE the branch
    # resolution below, which needs to know what this task's base actually is.
    meta = read_task_metadata(spec_dir)
    base = meta.get("base_branch") or meta.get("baseBranch") or "main"

    # On the kubejob path the build runs inside the k8s Job and pushes
    # aifactory/<spec_id>, while THIS control-plane worktree stays on the base
    # branch. Trusting its HEAD makes create_pr open a base->base PR, which gh
    # rejects ("head branch is the same as base"), so the auto-PR never opens
    # (#948, the PR-endgame twin of #938). A base branch is never a valid PR
    # head — use the canonical build branch the Job pushed.
    #
    # #980: this set used to be the literal {"HEAD", "main", "master"}, so on a
    # repo whose base is `dev` the guard never fired: `dev` was taken for the
    # build branch, and create_pr's `git fetch origin dev:dev` was then refused
    # by git ("refusing to fetch into branch 'refs/heads/dev' checked out at
    # ...") because that branch is checked out in this very worktree. The PR
    # never opened. Compare against THIS task's base, keeping main/master as a
    # backstop since a worktree sitting on either is never a valid head.
    if not branch or branch in {"HEAD", "main", "master", base}:
        # #1082: don't hardcode the aifactory/<spec> convention -- this was
        # the second, disagreeing branch resolver. Ask the canonical one
        # (marker first, then ref discovery); keep the convention only as the
        # last-resort backstop when nothing is discoverable, preserving the
        # historical behaviour for specs whose branch was never pushed.
        resolved, _reason = resolve_task_branch(
            worktree_path=worktree,
            project_path=project_path,
            spec_id=spec_id,
            base_branch=base,
        )
        branch = resolved or f"aifactory/{spec_id}"

    repo = ""
    try:
        req = json.loads((spec_dir / "requirements.json").read_text())
        gh = req.get("githubIssue") if isinstance(req, dict) else None
        if isinstance(gh, dict):
            repo = gh.get("repo") or gh.get("repository") or ""
        repo = repo or req.get("github_repo") or req.get("repo") or ""
    except (OSError, ValueError):
        pass
    if not repo:
        repo = meta.get("github_repo") or meta.get("githubRepo") or ""
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
    # The reference may be provider-qualified. Split it BEFORE anything treats it
    # as an owner/name: "gitlab:group/project" would otherwise reach _split_repo
    # and yield the owner "gitlab:group", which is a plausible-looking GitHub
    # repo that does not exist — the failure mode worth ruling out by parsing.
    provider, bare = _parse_repo_ref(repo)
    return {
        "worktree": worktree,
        "branch": branch,
        "base": base,
        "repo": bare,
        "provider": provider,
    }


async def run_pr_endgame(
    *,
    spec_dir: Path,
    spec_id: str,
    worktree: Path,
    branch: str,
    base: str,
    repo: str,
    provider: str = "github",
    auto_merge: bool = False,
    reviewer: str = "aifactory",
    review_fn: Callable[[], ReviewState] | None = None,
    fix_fn: Callable[[list], bool] | None = None,
    conflict_fixer: ConflictFixer | None = None,
    on_pr_opened: Callable[[int], None] | None = None,
    re_test: Callable[[], None] | None = None,
    runner: Runner = _default_runner,
    background: bool = True,
) -> dict:
    """Drive the endgame: open a PR, request a Copilot review, then (in the
    background) wait for Copilot's verdict and merge+re-test only on a clean
    Copilot approval when ``auto_merge`` is set.

    Caller guards with ``is_auto_pr_enabled(project_path)`` and resolves
    ``auto_merge`` via ``is_auto_merge_enabled(project_path)``. Returns once the
    PR is opened; the verdict-watch runs as a background task unless
    ``background=False`` (tests await it inline). Never raises.
    """
    # RFC-0020 3.5, Factory#366: this endgame is GITHUB-SHAPED and cannot be
    # pointed elsewhere. Every step below shells out to `gh` — create, request a
    # Copilot review, merge — and the canonical GitLab/Azure DevOps providers
    # raise NotImplementedError for enable_auto_merge anyway. So a non-GitHub
    # tenant is REFUSED here rather than allowed to run `gh pr create` against a
    # repo that is not on GitHub.
    #
    # Refused, and refused LOUDLY: the branch is already pushed by the build, the
    # reason is returned and logged, and the merge request is the tenant's to
    # open. Failing halfway through a `gh` call, or opening a PR on a
    # same-named GitHub repo that happens to exist, are both worse.
    if not _is_github(provider):
        logger.info(
            "[pr-endgame] skipping the auto-PR for %s: %s is not GitHub, and the "
            "endgame is gh-CLI-driven. Push is done; open the merge request there.",
            spec_id,
            provider,
        )
        return {
            "ok": False,
            "reason": "provider_not_github",
            "provider": provider,
            "repo": repo,
        }

    # RFC-0011 per-tier merge policy (#1158). `AIFACTORY_AUTO_MERGE` stays the
    # master switch; the tier may only ever make it STRICTER, never looser --
    # the same "may only tighten" rule merge_policy applies to its deployment
    # overlay. So a `factory:hard` task (reviewTier=blocking) cannot auto-merge
    # even with the flag on, while `factory:low` (auto) is unaffected.
    #
    # Narrowed here rather than at the call site because every route into the
    # endgame passes through this function, and `spec_dir` -- which is where
    # `task_metadata.json` lives -- is already a parameter.
    if auto_merge and not tier_allows_auto_merge(spec_dir):
        # The tier is logged through a fixed vocabulary, never raw: it comes
        # from a file on disk, and interpolating file content into a log line
        # lets a crafted value forge log entries (py/log-injection).
        logger.info(
            "[pr-endgame] %s: auto-merge withheld, reviewTier=%s does not permit "
            "it (AIFACTORY_AUTO_MERGE is on; the tier is stricter)",
            spec_id,
            _describe_tier(read_review_tier(spec_dir)),
        )
        auto_merge = False

    parts = _split_repo(repo)
    if parts is None:
        return {"ok": False, "reason": "no_repo", "repo": repo}
    owner, name = parts

    title, body = _pr_title_body(spec_dir, spec_id)
    try:
        pr = await asyncio.to_thread(
            lambda: create_pr(
                worktree=worktree,
                branch=branch,
                base=base,
                title=title,
                body=body,
                runner=runner,
            )
        )
    except Exception as exc:  # noqa: BLE001 — endgame must never break completion
        logger.warning("[pr-endgame] create_pr error: %s", exc)
        return {"ok": False, "reason": "create_pr_error", "error": str(exc)[:300]}
    if pr is None:
        return {"ok": False, "reason": "pr_not_created"}

    # Kick off the chosen reviewer now that the PR exists. Copilot is requested
    # via GitHub; the aifactory reviewer is triggered by the caller's
    # on_pr_opened (it has the review engine + project id).
    if reviewer == "copilot":
        await asyncio.to_thread(request_copilot_review, owner, name, pr, runner=runner)
    elif on_pr_opened is not None:
        try:
            on_pr_opened(pr)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[pr-endgame] on_pr_opened (reviewer trigger) failed: %s", exc
            )
    logger.info(
        "[pr-endgame] opened PR #%d for %s (reviewer=%s, auto_merge=%s)",
        pr,
        spec_id,
        reviewer,
        auto_merge,
    )

    coro = watch_and_finish(
        owner=owner,
        repo=name,
        pr=pr,
        auto_merge=auto_merge,
        require_copilot=(reviewer == "copilot"),
        review_fn=review_fn,
        fix_fn=fix_fn,
        conflict_fixer=conflict_fixer,
        worktree=str(worktree),
        base_branch=base,
        on_approved_merged=re_test,
        runner=runner,
    )
    if background:
        asyncio.create_task(coro)
        return {"ok": True, "pr": pr, "watching": True}
    result = await coro
    return {"ok": True, "pr": pr, "watching": False, **result}
