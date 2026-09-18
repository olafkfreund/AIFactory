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

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.project_registry import load_projects, resolve_project_path
from server.routes.task_service import get_spec_dirs
from server.services import pr_endgame as pe
from server.services.pr_endgame import Runner
from server.services.task_control import read_control, write_control
from server.tenancy import UNREADABLE_TENANT, spec_tenant

logger = logging.getLogger(__name__)


def _skip(task: str, reason: str) -> dict[str, Any]:
    return {"task": task, "action": "skipped", "pr": None, "reason": reason}


def _find_pr(
    owner: str, name: str, branch: str, runner: Runner
) -> tuple[bool, int | None, str | None]:
    """``(measured, pr_number, state)`` of the PR for ``branch``, in ANY state.

    An OPEN PR wins; otherwise the most recently created one, so a branch
    whose PR was MERGED or CLOSED is recognised as already decided (#2586).
    Without this, measuring the local branch would re-open squash-merged work:
    its commits survive locally under SHAs that ``main`` never received.

    ``measured`` is False when the ``gh pr list`` query itself failed --
    network blip, rate limit, bad token, unparseable output. That is NOT the
    same as "queried successfully and found nothing": treating a failed query
    as "no PR" is exactly the bug this module exists to not repeat (a failed
    measurement read as a definite negative), so the caller must skip rather
    than proceed to ``gh pr create`` on an unmeasured idempotency check.
    """
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
            "all",
            "--json",
            "number,state,createdAt",
        ],
        None,
    )
    if not res.ok:
        return False, None, None
    try:
        prs = json.loads(res.out or "[]")
    except ValueError:
        return False, None, None
    if not isinstance(prs, list):
        return False, None, None
    prs = [p for p in prs if isinstance(p, dict) and isinstance(p.get("number"), int)]
    if not prs:
        return True, None, None
    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    pick = (
        open_prs[0] if open_prs else max(prs, key=lambda p: str(p.get("createdAt", "")))
    )
    return True, int(pick["number"]), str(pick.get("state") or "")


def _pick_ref(
    worktree: Path, branch: str, on_origin: bool, runner: Runner
) -> str | None:
    """The ref that holds this task's work, or None when that cannot be decided.

    #2586: the build on the co-mount path commits in this worktree and never
    pushes, so ``origin/<branch>`` can sit at the base while the local branch
    holds all the work. On the packed path the reverse happens: the Job pushed
    to origin and the local ref here is stale. So take whichever ref CONTAINS
    the other. If they have diverged, neither is safe to push or PR without a
    force, and a force can destroy work -- that is a human's call.
    """
    local = f"refs/heads/{branch}"
    remote = f"origin/{branch}"
    has_local = runner(
        ["git", "rev-parse", "--verify", "--quiet", local], str(worktree)
    ).ok
    if not has_local:
        return remote if on_origin else None
    if not on_origin:
        return local

    def contains(ancestor: str, ref: str) -> bool:
        return runner(
            ["git", "merge-base", "--is-ancestor", ancestor, ref], str(worktree)
        ).ok

    if contains(remote, local):
        return local
    if contains(local, remote):
        return remote
    raise _SkipTask("diverged (local and origin branch both have unique commits)")


def _branch_ahead_and_changed(
    worktree: Path, base: str, branch: str, runner: Runner
) -> tuple[int | None, int | None, int | None]:
    """``(ahead_by, changed_files, unpushed)`` of the task's work over ``base``.

    Measures the ref that holds the work (see ``_pick_ref``), not merely the
    origin copy: #2586 found five tasks whose commits existed only in the
    local branch, which the origin-only measurement reported as
    ``no_content`` -- dropping real work while claiming there was none.
    ``changed_files`` is a three-dot diff, so changes that reached ``base``
    after the branch point are not counted. ``unpushed`` is how many of those
    commits origin does not have yet (``create_pr`` pushes them).

    ``(None, None, None)`` means unmeasurable (base not fetchable, no branch
    anywhere, or git refused) -- the caller must treat that as "don't know",
    never as "empty" (#5 is about a MEASURED ahead_by of 0).
    """
    unmeasurable = (None, None, None)
    if not runner(["git", "fetch", "origin", base], str(worktree)).ok:
        return unmeasurable
    on_origin = runner(["git", "fetch", "origin", branch], str(worktree)).ok
    ref = _pick_ref(worktree, branch, on_origin, runner)
    if ref is None:
        return unmeasurable

    def count(rng: str) -> int | None:
        res = runner(["git", "rev-list", "--count", rng], str(worktree))
        text = res.out.strip()
        return int(text) if res.ok and text.isdigit() else None

    ahead = count(f"origin/{base}..{ref}")
    unpushed = count(f"origin/{branch}..{ref}") if on_origin else ahead
    changed = runner(
        ["git", "diff", "--name-only", f"origin/{base}...{ref}"], str(worktree)
    )
    if ahead is None or unpushed is None or not changed.ok:
        # A failed diff or count must not be read as "measured, zero" --
        # that reported a real branch as empty. Unmeasurable in ANY
        # dimension means the whole triple is unmeasurable.
        return unmeasurable
    changed_files = len([line for line in changed.out.splitlines() if line.strip()])
    return ahead, changed_files, unpushed


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
        # Explicit annotation, not a bare return: this file's mypy_path scope
        # makes the lazily-imported call itself Any (same gap noted beside
        # `_should_require_human_review` in coder.py) -- the declared type is
        # what keeps `-> str | None` honest against `no-any-return`.
        evidence: str | None = trailing_gate_evidence(spec_dir, project_path)
    except Exception:  # noqa: BLE001 - evidence lookup must never break the sweep
        logger.debug(
            "[merger] gate evidence lookup failed for %s", spec_dir.name, exc_info=True
        )
        return None
    return evidence


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
    title, _unused_body = pe._pr_title_body(spec_dir, spec_id)
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

    pr_list_measured, existing, state = _find_pr(owner, name, branch, runner)
    if not pr_list_measured:
        raise _SkipTask("open_pr_check_unmeasurable (gh pr list failed)")
    if existing is not None:
        # A merged or closed PR is a decision already made by a human: never
        # open a second PR for the same branch (#2586).
        action = {"MERGED": "merged", "CLOSED": "closed"}.get(
            state or "", "already_open"
        )
        return {"action": action, "pr": existing}

    ahead_by, changed_files, unpushed = _branch_ahead_and_changed(
        ctx["worktree"], base, branch, runner
    )
    if ahead_by is None:
        raise _SkipTask("ahead_by_unmeasurable (branch not found locally or on origin)")
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
            "unpushed": unpushed,
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
    except Exception as exc:
        logger.warning("[merger] create_pr error for %s: %s", spec_id, exc)
        raise _SkipTask(f"create_pr_error:{exc}") from exc
    if pr is None:
        raise _SkipTask("pr_not_created (gh pr create failed)")
    return {"action": "opened", "pr": pr, "unpushed": unpushed}


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
        outcome = _decide(
            project_path, spec_dir, spec_id, dry_run=dry_run, runner=runner
        )
    except _SkipTask as skip:
        return _skip(task, skip.reason)
    return {"task": task, "reason": None, **outcome}


# #2586: what a task's board status should say once its PR state is known.
# ``None`` as the reason means "clear it". Only these outcomes are decisive;
# every other skip (unmeasurable, diverged, ...) says nothing about the task,
# so its status is left exactly as it was.
_STATUS_FOR_ACTION: dict[str, tuple[str, str | None]] = {
    "opened": ("human_review", "awaiting_merge"),
    "already_open": ("human_review", "awaiting_merge"),
    "merged": ("done", None),
    "closed": ("human_review", "pr_closed"),
}
_NO_WORK = ("human_review", "no_work")


def _sync_status(spec_dir: Path, result: dict[str, Any]) -> bool:
    """Make the task's board status follow its PR. True when it wrote.

    Writes only over ``human_review``: a status a human set (dragged to
    ``done``, back to ``backlog``) or a task still ``in_progress`` is never
    overwritten -- the merger reports, it does not overrule. An unchanged
    target is not rewritten, so ``status_written`` counts real changes.
    Never raises: a failed write must not turn a PR outcome into an error.
    """
    action = result.get("action")
    if action == "skipped" and str(result.get("reason") or "").startswith("no_content"):
        target: tuple[str, str | None] | None = _NO_WORK
    else:
        target = _STATUS_FOR_ACTION.get(str(action))
    if target is None:
        return False
    status, reason = target
    try:
        current = read_control(spec_dir)
        if current.get("status") != "human_review":
            return False
        if current.get("status") == status and current.get("reviewReason") == reason:
            return False
        write_control(
            spec_dir,
            status=status,
            review_reason=reason,
            clear_review_reason=reason is None,
            updated_by="merger",
        )
    except Exception:
        # A status write never breaks the sweep; logged with its traceback.
        logger.exception("[merger] status sync failed for %s", spec_dir.name)
        return False
    return True


def process_one(
    project_id: str,
    project_path: Path,
    spec_dir: Path,
    *,
    runner: Runner = pe._default_runner,
) -> dict[str, Any]:
    """Land one task: open its PR if due, then make its status follow the PR.

    The build-end entry point (#2586) -- the same decision the sweep makes,
    for a single spec, for real (never a dry run).
    """
    result = _process_spec(
        project_id, project_path, spec_dir, dry_run=False, runner=runner
    )
    result["status_written"] = _sync_status(spec_dir, result)
    return result


def sweep(
    *,
    dry_run: bool = True,
    runner: Runner = pe._default_runner,
    project_ids: Iterable[str] | None = None,
    tenant: str | None = None,
) -> dict[str, Any]:
    """Scan every project's specs and open PRs for stranded branches.

    ``project_ids``, when given, restricts the scan to those ids (the route
    passes the caller's visible projects -- see ``routes/merger.py`` -- so a
    non-admin user cannot trigger a fleet-wide scan of orgs they cannot see).
    Defaults to every registered project.

    ``tenant``, when given, further restricts to specs stamped with that
    tenant (mirrors ``routes/tasks.py``'s ``list_tasks`` -- #1554 finding 1:
    org membership alone does not separate tenants sharing one org, so a spec
    with no stamp is never matched by a real tenant). A spec whose stamp
    exists but could not be read is never matched either -- it fails closed
    (``tenancy.UNREADABLE_TENANT``) rather than defaulting, and is recorded
    as a skip, not silently dropped. ``None`` means every tenant: that is
    what the route passes whenever multi-tenant mode itself is OFF
    (``routes/merger.py``'s ``_tenant_scope`` checks only
    ``multi_tenant_enabled()``, not who the caller is -- a service-principal
    or auth-disabled call still gets tenant-filtered like anyone else once
    multi-tenant mode is on and it sends an ``X-Tenant-Id``).

    Returns a report with one entry per spec examined -- ``opened``,
    ``already_open``, ``would_open`` (dry run), or ``skipped`` with a reason --
    plus counts. Safe to re-run: an already-open PR is left alone, and a
    content-free branch is never opened. Never merges anything.
    """
    results: list[dict[str, Any]] = []
    scope = load_projects() if project_ids is None else project_ids
    for project_id in scope:
        try:
            project_path = resolve_project_path(project_id)
        except Exception:  # noqa: BLE001 - one broken project must not hide the rest
            logger.warning("[merger] cannot resolve project %s", project_id)
            continue
        try:
            spec_dirs = get_spec_dirs(project_path)
        except Exception:
            # hide every project after this one -- #1554 finding 4, the exact
            # "never drop work" rule this module exists to enforce.
            logger.exception(
                "[merger] cannot enumerate specs for project %s", project_id
            )
            results.append(
                _skip(f"{project_id}:*", "spec_enumeration_error (see logs)")
            )
            continue
        if tenant is not None:
            kept = []
            for d in spec_dirs:
                stamp = spec_tenant(d)
                if stamp == UNREADABLE_TENANT:
                    # Fail closed (#1554 finding 1, generalised): an
                    # unreadable stamp must never be treated as any
                    # tenant's, including "default" -- that would let a
                    # default-tenant sweep push/PR a spec whose real tenant
                    # is unknown. Recorded, not silently dropped, same as
                    # every other unmeasurable git state this module skips.
                    results.append(
                        _skip(
                            f"{project_id}:{d.name}",
                            "tenant_stamp_unreadable (see logs)",
                        )
                    )
                    continue
                if stamp == tenant:
                    kept.append(d)
            spec_dirs = kept
        for spec_dir in spec_dirs:
            try:
                result = _process_spec(
                    project_id,
                    project_path,
                    spec_dir,
                    dry_run=dry_run,
                    runner=runner,
                )
                if not dry_run:
                    result["status_written"] = _sync_status(spec_dir, result)
                results.append(result)
            except Exception:
                logger.exception(
                    "[merger] sweep failed for %s:%s", project_id, spec_dir.name
                )
                results.append(
                    _skip(f"{project_id}:{spec_dir.name}", "sweep_error (see logs)")
                )

    counts = {
        "opened": 0,
        "already_open": 0,
        "would_open": 0,
        "merged": 0,
        "closed": 0,
        "skipped": 0,
        "status_written": 0,
    }
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        counts["status_written"] += bool(r.get("status_written"))
    return {"dry_run": dry_run, "results": results, "counts": counts}


# ── backstop loop (Factory#2586) ─────────────────────────────────────────────
#
# In-process, like services/stale_reaper: the spec tree lives on an RWO
# local-path PVC, so a CronJob pod scheduled off-node would see no specs,
# open nothing and exit green. The build-end hook in completion_orchestration
# is the primary trigger; this catches what it cannot (failed builds with
# real work, builds that finished before this shipped, a hook that errored).

_DEFAULT_INTERVAL_S = 900.0
_last_tick_at: str | None = None


def merger_sweep_enabled() -> bool:
    """Off unless explicitly switched on, like the other lifespan loops."""
    return os.environ.get("AIFACTORY_MERGER_SWEEP", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def dry_run() -> bool:
    """Anything other than an explicit "false" means report-only.

    Fails closed on a typo, like the reaper: a typo that reports wastes a
    tick, a typo that writes pushes branches and opens PRs.
    """
    raw = os.environ.get("AIFACTORY_MERGER_SWEEP_DRY_RUN", "true")
    return raw.strip().lower() != "false"


def interval_s() -> float:
    raw = os.environ.get("AIFACTORY_MERGER_SWEEP_INTERVAL_S", "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        value = 0.0
    if value <= 0:
        # A zero or unparseable interval would spin against `gh`.
        logger.warning(
            "AIFACTORY_MERGER_SWEEP_INTERVAL_S=%r is not a positive number; using %s",
            raw,
            _DEFAULT_INTERVAL_S,
        )
        return _DEFAULT_INTERVAL_S
    return value


def last_tick_at() -> str | None:
    """When the loop last completed a sweep; None if it never has.

    job-watchdog cannot see an in-process loop, so this is how a dead one
    shows: the GET report returns it, and a stale value means no tick.
    """
    return _last_tick_at


def sweep_once() -> dict[str, Any]:
    """One loop tick. The whole report goes to the log as one JSON line."""
    global _last_tick_at  # noqa: PLW0603 - the loop's one piece of state
    report = sweep(dry_run=dry_run())
    logger.info("merger-sweep %s", json.dumps(report, sort_keys=True, default=str))
    _last_tick_at = datetime.now(UTC).isoformat()
    return report


async def merger_loop(*, stop: asyncio.Event | None = None) -> None:
    """One sweep per interval until stopped. A failed tick never kills it."""
    stop = stop or asyncio.Event()
    interval = interval_s()
    while not stop.is_set():
        try:
            # Shells out to git/gh per spec: blocking, so off the event loop.
            await asyncio.to_thread(sweep_once)
        except Exception:
            logger.exception("merger-sweep tick failed; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
