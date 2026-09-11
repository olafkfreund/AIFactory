"""The merger: get finished work in front of a human (Factory#1027-adjacent).

Eight tasks ran since the verification fixes. Seven produced real committed
work on pushed branches. Zero opened a PR -- ``services.pr_endgame`` only
opens one as a side-effect of a build ``update_qa_status(status="approved")``
call, and QA correctly *refuses* to approve when it cannot find gate evidence
(the #1496 guard). So the PR -- the thing a human would actually find the work
through -- never happens, even though the work is real and pushed.

This module is the other shape: not a callback on one build finishing, but an
idempotent SWEEP that can be re-run at any time. It looks at every task spec
in every project, and for each one whose branch carries real, unmerged commits
with no open PR, opens one -- honestly. It never requires QA approval (a PR is
a review surface, not a certificate) and it never merges (that stays human;
``AIFACTORY_AUTO_MERGE`` is untouched and unread here).

Every branch is accounted for in the returned report: opened, already open, or
skipped with a reason. Silence is the bug this fixes, so nothing here may
process a branch and say nothing about it.

Heavily reuses ``services.pr_endgame``: ``gather_pr_context`` for worktree/
branch/base/repo/provider resolution, ``create_pr`` for the push+``gh pr
create`` mechanics, ``_is_github``/``_split_repo`` for the same provider
gating ``run_pr_endgame`` itself applies. This module adds only what
``pr_endgame`` does not have: iterating every stranded task, checking for an
existing open PR (idempotency), checking the branch actually has content
(#5), and writing an honest — not "clean, QA-passed" — PR body.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from server.project_registry import load_projects, resolve_project_path
from server.routes.task_service import get_spec_dirs
from server.services import pr_endgame as pe
from server.services.pr_endgame import Runner

logger = logging.getLogger(__name__)


def _skip(task: str, reason: str) -> dict[str, Any]:
    return {"task": task, "action": "skipped", "pr": None, "reason": reason}


def _find_open_pr(owner: str, name: str, branch: str, runner: Runner) -> int | None:
    """An already-open PR for ``branch``, or None. Makes re-runs idempotent."""
    res = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{owner}/{name}",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[0].number",
        ],
        None,
    )
    text = res.out.strip() if res.ok else ""
    return int(text) if text.isdigit() else None


def _branch_ahead_and_changed(
    worktree: Path, base: str, branch: str, runner: Runner
) -> tuple[int | None, int | None]:
    """``(ahead_by, changed_files)`` of ``branch`` over ``base``, measured locally.

    Mirrors what GitHub's ``compare/{base}...{head}`` reports (``ahead_by`` and
    changed file count), but from ``git`` directly so no PR needs to exist yet
    to measure it. Both fetched fresh from origin, since the build pushed the
    branch but this worktree may never have seen it. ``(None, None)`` means
    unmeasurable (branch not on origin, or git refused) -- the caller must
    treat that as "don't know", never as "empty" (#5 is about a MEASURED
    ahead_by of 0, not an absent measurement).
    """
    runner(["git", "fetch", "origin", base], str(worktree))
    fetched_head = runner(["git", "fetch", "origin", branch], str(worktree))
    if not fetched_head.ok:
        return None, None
    base_ref, head_ref = f"origin/{base}", f"origin/{branch}"
    ahead = runner(["git", "rev-list", "--count", f"{base_ref}..{head_ref}"], str(worktree))
    if not ahead.ok or not ahead.out.strip().isdigit():
        return None, None
    changed = runner(
        ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"], str(worktree)
    )
    changed_files = (
        len([line for line in changed.out.splitlines() if line.strip()])
        if changed.ok
        else None
    )
    return int(ahead.out.strip()), changed_files


def _gate_evidence_for(spec_dir: Path, project_path: Path) -> str | None:
    """The #1496 recorded gate evidence for this build, best-effort.

    Lazily imported: this module (like ``pr_endgame``) is reachable both with
    and without the backend package on ``sys.path``. Absence of the import, or
    of the evidence itself, must never break the sweep -- it just means the PR
    body honestly says no gate evidence is recorded.
    """
    try:
        from agents.gate_runner import trailing_gate_evidence  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return trailing_gate_evidence(spec_dir, project_path)
    except Exception:  # noqa: BLE001 - evidence lookup must never break the sweep
        logger.debug(
            "[merger] gate evidence lookup failed for %s", spec_dir.name, exc_info=True
        )
        return None


def _qa_status_for(spec_dir: Path) -> str:
    """The recorded ``qa_signoff.status`` from ``implementation_plan.json``.

    ``"not run"`` when the file, or the sign-off, is absent or unreadable --
    the honest reading of "QA never recorded anything here", distinct from a
    recorded ``"rejected"`` or ``"pending"``.
    """
    try:
        plan = json.loads((spec_dir / "implementation_plan.json").read_text())
    except (OSError, ValueError):
        return "not run"
    signoff = plan.get("qa_signoff") if isinstance(plan, dict) else None
    status = signoff.get("status") if isinstance(signoff, dict) else None
    return str(status) if status else "not run"


def _issue_number(spec_dir: Path) -> int | None:
    """The origin issue this task was raised from, for a ``Fixes #N`` line."""
    try:
        req = json.loads((spec_dir / "requirements.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(req, dict):
        return None
    prov = req.get("provenance")
    if isinstance(prov, dict) and isinstance(prov.get("issue_number"), int):
        return int(prov["issue_number"])
    gh = req.get("githubIssue")
    if isinstance(gh, dict) and isinstance(gh.get("number"), int):
        return int(gh["number"])
    return None


def honest_pr_title_and_body(
    spec_dir: Path, spec_id: str, project_path: Path, review_tier: str | None
) -> tuple[str, str]:
    """An honest PR title/body: what ran, what it said, never "verified".

    Reuses ``pr_endgame._pr_title_body`` for the title (same requirements.json
    read everything else here needs anyway) but writes its own body -- the
    ``pr_endgame`` body says "clean, QA-passed build", which is exactly the
    claim this module must never make: these branches have NOT gone through
    that gate, that is the whole reason the merger exists. A reviewer must be
    able to tell "the factory opened this" from "the factory verified this".
    """
    title, _unused_body = pe._pr_title_body(spec_dir, spec_id)  # noqa: SLF001
    gate_line = _gate_evidence_for(spec_dir, project_path) or (
        "no verification gates recorded for this build"
    )
    qa_status = _qa_status_for(spec_dir)
    tier_line = f"\n- Review tier: {review_tier}" if review_tier else ""
    issue = _issue_number(spec_dir)
    fixes_line = f"\n\nFixes #{issue}" if issue is not None else ""
    body = (
        "Opened by AIFactory's merger: this task has committed, pushed work "
        "with no open pull request.\n\n"
        "**This PR is a review surface, not a certificate.** The factory did "
        "not require QA approval to open it -- review the code as you would "
        "any other PR.\n\n"
        "**Verification status:**\n"
        f"- Gate evidence: {gate_line}\n"
        f"- QA sign-off: {qa_status}"
        f"{tier_line}"
        f"{fixes_line}"
    )
    return title, body


class _SkipTask(Exception):  # noqa: N818 - not an error, a control-flow signal
    """Raised to short-circuit ``_decide`` with a reason, keeping its return
    count under the complexity ratchet's cap without collapsing the many
    distinct "why not" cases into one another."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _decide(
    project_path: Path,
    spec_dir: Path,
    spec_id: str,
    *,
    dry_run: bool,
    runner: Runner,
) -> dict[str, Any]:
    """Everything about one spec except actually opening the PR.

    Returns the outcome dict directly for ``already_open``/``would_open``/
    ``opened``, or raises ``_SkipTask`` for every "no PR, here's why" case --
    ``_process_spec`` converts that into the same-shaped ``skipped`` dict.
    """
    ctx = pe.gather_pr_context(project_path, spec_dir, spec_id, runner=runner)
    if ctx is None:
        raise _SkipTask("no_worktree_or_resolvable_repo")
    if not pe._is_github(ctx["provider"]):
        raise _SkipTask(f"provider_not_github:{ctx['provider']}")
    parts = pe._split_repo(ctx["repo"])
    if parts is None:
        raise _SkipTask(f"unresolvable_repo:{ctx['repo']}")
    owner, name = parts
    branch, base = ctx["branch"], ctx["base"]

    existing = _find_open_pr(owner, name, branch, runner)
    if existing is not None:
        return {"action": "already_open", "pr": existing}

    ahead_by, changed_files = _branch_ahead_and_changed(
        ctx["worktree"], base, branch, runner
    )
    if ahead_by is None:
        raise _SkipTask("ahead_by_unmeasurable (branch not fetchable from origin)")
    if ahead_by == 0 or not changed_files:
        raise _SkipTask(
            f"no_content (ahead_by={ahead_by}, changed_files={changed_files or 0})"
        )
    if not pe.is_auto_pr_enabled(project_path):
        raise _SkipTask("auto_pr_disabled")

    if dry_run:
        return {
            "action": "would_open",
            "pr": None,
            "ahead_by": ahead_by,
            "changed_files": changed_files,
        }

    title, body = honest_pr_title_and_body(
        spec_dir, spec_id, project_path, ctx.get("review_tier")
    )
    try:
        pr = pe.create_pr(
            worktree=ctx["worktree"],
            branch=branch,
            base=base,
            title=title,
            body=body,
            runner=runner,
        )
    except Exception as exc:  # noqa: BLE001 - the sweep must never crash on one task
        logger.warning("[merger] create_pr error for %s: %s", spec_id, exc)
        raise _SkipTask(f"create_pr_error:{exc}") from exc
    if pr is None:
        raise _SkipTask("pr_not_created (gh pr create failed)")
    return {"action": "opened", "pr": pr}


def _process_spec(
    project_id: str,
    project_path: Path,
    spec_dir: Path,
    *,
    dry_run: bool,
    runner: Runner,
) -> dict[str, Any]:
    spec_id = spec_dir.name
    task = f"{project_id}:{spec_id}"
    try:
        outcome = _decide(project_path, spec_dir, spec_id, dry_run=dry_run, runner=runner)
    except _SkipTask as skip:
        return _skip(task, skip.reason)
    return {"task": task, "reason": None, **outcome}


def sweep(*, dry_run: bool = True, runner: Runner = pe._default_runner) -> dict[str, Any]:
    """Scan every project's specs and open PRs for stranded branches.

    Returns a report with one entry per spec examined -- ``opened``,
    ``already_open``, ``would_open`` (dry run), or ``skipped`` with a reason --
    plus counts. Safe to re-run: an already-open PR is left alone, and a
    content-free branch is never opened. Never merges anything.
    """
    results: list[dict[str, Any]] = []
    for project_id in load_projects():
        try:
            project_path = resolve_project_path(project_id)
        except Exception:  # noqa: BLE001 - one broken project must not hide the rest
            logger.warning("[merger] cannot resolve project %s", project_id)
            continue
        for spec_dir in get_spec_dirs(project_path):
            try:
                results.append(
                    _process_spec(
                        project_id, project_path, spec_dir, dry_run=dry_run, runner=runner
                    )
                )
            except Exception:  # noqa: BLE001 - one broken spec must not hide the rest
                logger.exception(
                    "[merger] sweep failed for %s:%s", project_id, spec_dir.name
                )
                results.append(
                    _skip(f"{project_id}:{spec_dir.name}", "sweep_error (see logs)")
                )

    counts = {"opened": 0, "already_open": 0, "would_open": 0, "skipped": 0}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {"dry_run": dry_run, "results": results, "counts": counts}
