"""
QA Management Tools
===================

Tools for managing QA status and sign-off in implementation_plan.json.
"""

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.gate_runner import (
    evidence_shows_an_executed_gate,
    gate_dir_for,
    gate_outcomes_include_a_failure,
    trailing_gate_evidence,
    trailing_gate_marker_is_current,
)

from .api_contract import missing_exports

try:
    from claude_agent_sdk import tool

    SDK_TOOLS_AVAILABLE = True
except ImportError:
    SDK_TOOLS_AVAILABLE = False
    tool = None


def _missing_contract_exports(spec_dir: Path, project_dir: Path) -> list[str]:
    """Required names from spec.md that this build did not define (#1421).

    Best-effort: an unreadable spec yields no contract and no refusal. An
    unreadable spec is not evidence that a contract was broken, and blocking
    sign-off on it would trade a false pass for a false failure — the same trade
    ``_nothing_was_built`` refuses to make when git cannot answer.
    """
    try:
        spec_text = (spec_dir / "spec.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return missing_exports(spec_text, project_dir)


def _nothing_was_built(project_dir: Path) -> str | None:
    """Explain why this worktree shows no build output, or None if it does.

    Two signals, either of which is evidence of work: a commit that is not yet
    on any origin branch, or an uncommitted change. Both empty means the build
    produced nothing.

    ``--not --remotes=origin`` is what makes this work without knowing the base
    branch. The worktree is cut from a base commit that IS on origin, so that
    commit is excluded and only what this build added is counted. Asking for the
    base by name would mean resolving it here, and a wrong guess would make the
    check silently vacuous -- the failure mode being fixed.

    Returns a human-readable reason (truthy) when nothing was built, so the
    caller can put it in the refusal. Returns None when there is output, and
    also when git itself cannot answer: an unavailable git is not evidence of an
    empty build, and blocking every sign-off on it would trade a false pass for
    a false failure.
    """
    try:
        commits = subprocess.run(
            ["git", "rev-list", "--count", "HEAD", "--not", "--remotes=origin"],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if commits.returncode != 0 or dirty.returncode != 0:
        return None

    n = commits.stdout.strip()
    if n and n != "0":
        return None
    if dirty.stdout.strip():
        return None

    return f"{project_dir} has no commits beyond its base and no uncommitted changes."


# A stale-but-once-passing marker means gates genuinely ran here before, so a
# refresh is worth the cost -- but the check ("is the marker stale?") and the
# act (dispatch a fresh gate run) are not atomic on their own: two concurrent
# `update_qa_status(approved)` calls for the SAME spec_dir can both observe
# staleness before either writes a fresh marker, and both dispatch a full gate
# Job for the same HEAD (Copilot review, PR #1547). The claim file below makes
# "start a refresh" an atomic, filesystem-level compare-and-set via
# `O_CREAT | O_EXCL` -- the same primitive a lockfile uses -- so at most one
# refresh is ever in flight per spec_dir, whether the two callers are two
# `asyncio` tasks in the same process or two separate backend processes
# sharing the same spec_dir (both are possible: a QA retry iteration can
# overlap the previous one in-process, and the spec_dir itself lives on the
# same shared filesystem the marker does, so nothing about this scopes it to
# one process -- an in-memory lock would only cover the former).
#
# #1549 review (Copilot): a claim stored INSIDE `spec_dir` gets caught by spec
# replication and leaks. Two mechanisms copy spec-dir CONTENTS wholesale --
# `WorktreeSyncMixin._sync_worktree_files`'s "sync any additional file" loop
# (apps/web-server/server/services/agent_worktree_sync.py) copies every
# regular file `worktree_spec.iterdir()` finds that isn't on the hardcoded
# `files_to_sync` allowlist, dotfiles included, worktree -> main; and
# `copy_spec_to_worktree` (apps/backend/core/workspace/setup.py) does an
# unconditional `shutil.copytree(source_spec_dir, target_spec_dir,
# dirs_exist_ok=True)` for every NEW worktree, main -> worktree. Both read
# from disk, not from a name list this module controls, so a claim living at
# `spec_dir / name` survives past its `finally` release in whichever copy the
# sync ran before that release landed -- turning the TTL backstop (meant for a
# crashed run) into the everyday path: an hour-long block on every refresh,
# for a perfectly healthy claim that was simply copied elsewhere.
#
# `.trailing_gates_done` itself tolerates this because it is DATA about the
# tree (bound to a HEAD sha, re-validated per read -- #1545); a copy of it is
# either still valid or correctly stale, never actively harmful. A claim has
# no such property: it is pure per-attempt, per-process coordination state,
# never meant to be read anywhere but the process that just wrote it, so a
# copy of it is never "correctly" anything -- it is just a phantom lock. The
# established in-repo pattern for adjacent problems (`task_control.json`,
# `qa_review_cycle.json` -- see their own module docstrings) is a NAME
# exclusion from the sync allowlist, but that only protects the one sync
# direction with a hardcoded list (`files_to_sync`); it does nothing against
# the catch-all "any other file" loop, and nothing against
# `copy_spec_to_worktree`'s unconditional copytree, and it is one more name
# every future replication path has to remember to add. Storing the claim as
# a SIBLING of `spec_dir` instead of a child is immune to both current paths
# structurally, not by convention: `copytree(source_spec_dir, ...)` only ever
# copies `source_spec_dir`'s own contents, and the "any other file" loop only
# ever iterates `worktree_spec.iterdir()`'s own contents -- neither one visits
# `spec_dir`'s siblings, so nothing has to be told to skip this file, and
# nothing new added later needs to remember to either.
_GATE_REFRESH_CLAIM_SUFFIX = ".trailing_gates_refresh.claim"

# Generous upper bound on one refresh's real duration: a build's trailing
# gates can fan out across several languages, sequentially, and #1541 measured
# that fan-out at up to ~60 minutes on a cold Nix store. A claim older than
# this was abandoned by a run that crashed before its `finally` released it
# (OOM-kill, node eviction) -- treated as reclaimable rather than a permanent
# lock, since a stale claim that blocks every future refresh forever would be
# worse than the race it exists to prevent.
_STALE_CLAIM_TTL_SECONDS = 3600


def _gate_refresh_claim_path(spec_dir: Path) -> Path:
    """Where `spec_dir`'s refresh claim lives: a SIBLING of `spec_dir`, named
    from `spec_dir.name` so distinct specs sharing a parent never collide,
    not a child of it -- see the module note above for why."""
    return spec_dir.parent / f".{spec_dir.name}{_GATE_REFRESH_CLAIM_SUFFIX}"


def _claim_gate_refresh(spec_dir: Path) -> bool:
    """Atomically claim the right to refresh `spec_dir`'s trailing gates.

    True means the caller now owns the claim and must release it (see
    `_release_gate_refresh_claim`) once its refresh attempt finishes, success
    or not. False means another refresh already owns it -- the caller must
    not dispatch a gate run, and should treat evidence as still stale (the
    in-flight refresh, or a future approval attempt after it, will make
    current evidence available).
    """
    claim = _gate_refresh_claim_path(spec_dir)
    if _create_claim_file(claim):
        return True

    try:
        age_seconds = time.time() - claim.stat().st_mtime
    except OSError:
        return False  # raced with the owner's own release; treat as "not mine"
    if age_seconds <= _STALE_CLAIM_TTL_SECONDS:
        return False  # a live refresh owns it

    # Abandoned by a crashed run. Reclaim once; if another caller reclaims it
    # first, that caller owns it now and this one backs off, same as above.
    try:
        claim.unlink()
    except OSError:
        return False
    return _create_claim_file(claim)


def _create_claim_file(claim: Path) -> bool:
    """The actual `O_CREAT | O_EXCL` compare-and-set, shared by both attempts
    in `_claim_gate_refresh` (first try, and the one retry after reclaiming an
    expired claim) so the atomicity lives in exactly one place."""
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _release_gate_refresh_claim(spec_dir: Path) -> None:
    """Release a claim this process holds. Best-effort: a missing file (this
    process's own prior release, or a TTL reclaim by someone else racing the
    unlikely case where BOTH the TTL expired and this run is still alive) is
    not an error -- the goal state (no claim left behind) is already true."""
    with contextlib.suppress(OSError):
        _gate_refresh_claim_path(spec_dir).unlink()


async def _refresh_stale_gate_evidence(spec_dir: Path, project_dir: Path) -> str | None:
    """Re-run the trailing gates once when their evidence went stale (#1546+1).

    Fires ONLY when a marker already EXISTS but no longer binds to the tree's
    current HEAD -- i.e. gates ran successfully at least once for this build,
    and a commit landed afterwards (a QA-fixer commit, a QA agent's own
    trivial normalization -- flake.nix, a stray flake.lock) that invalidated
    it (#1545). A build whose gate step never ran at all is left untouched
    here: retrying a genuinely absent toolchain on every approval attempt
    would spend a full Nix Job, repeatedly, on a spec that never had one --
    for no better answer than the refusal already gives.

    Delegates to the coder's own trailing-gate runner
    (``agents.coder._run_trailing_gates_if_build_complete``) -- the SAME
    dispatch that produced the ORIGINAL marker, so a refreshed run is bound
    by the same tree-binding rule (#1545) and writes through the same marker
    format.

    #1548 (Copilot review of #1547): staleness here is a READ ("is the marker
    current?"); dispatching the coder's runner is a separate WRITE, and the
    two are not atomic. Two `update_qa_status(approved)` calls for the same
    spec_dir -- two overlapping `asyncio` tasks in one process, or two
    processes sharing the same spec_dir on the PVC -- could both read "stale"
    before either write landed, and both dispatch a full gate Job for the
    same HEAD. `_claim_gate_refresh` closes that window: only the caller that
    wins the claim dispatches; the other backs off and returns None (still
    stale, from its point of view) rather than racing the dispatch itself.
    """
    marker = spec_dir / ".trailing_gates_done"
    if not marker.exists():
        return None
    gate_dir = gate_dir_for(spec_dir, project_dir)
    if trailing_gate_marker_is_current(spec_dir, gate_dir):
        return None  # already current -- nothing to refresh

    if not _claim_gate_refresh(spec_dir):
        return None  # another refresh already owns this spec_dir; back off

    try:
        from agents.coder import _run_trailing_gates_if_build_complete  # noqa: PLC0415

        await _run_trailing_gates_if_build_complete(spec_dir, project_dir)
    finally:
        _release_gate_refresh_claim(spec_dir)

    # Explicit annotation, not a bare return: `trailing_gate_evidence` type-checks
    # fine on its own, but this file's mypy_path scope makes the call itself Any
    # (see the `bool()` cast note on `_should_require_human_review` in coder.py
    # for the same gap) -- the declared type here is what keeps `-> str | None`
    # honest against `no-any-return`.
    evidence: str | None = trailing_gate_evidence(spec_dir, project_dir)
    return evidence


async def _approval_refusal_reason(spec_dir: Path, project_dir: Path) -> str | None:
    """Why `update_qa_status(status="approved")` must be refused, or None.

    Every guard an "approved" write has to pass, in one place, so
    `update_qa_status` itself stays a thin dispatcher rather than a wall of
    early returns (the ratchet's complexity caps exist for exactly that).

    #1396: nothing was built -- a build with no diff cannot have been tested,
    whatever `tests_passed` claims.

    #1421: the spec enumerates an API this build does not export -- a green
    suite against an invented API is indistinguishable from one against the
    right one.

    #1496: no evidence a verification command actually ran. Observed live,
    twice: the coder agent said outright it could NOT run the suite ("no
    Gradle/JVM on PATH ... I cannot execute the suite") and this tool was
    still called with status="approved". "APPROVED ✓" is the same string a
    build gets when its tests ran and passed -- so the distinction between
    *verified* and *read carefully* was destroyed at exactly the point a
    human (or a downstream dashboard) reads the result. `trailing_gate_evidence`
    reads the one OBJECTIVE record of whether a verification command
    executed: the marker the coder's own trailing-gate step writes to this
    same spec_dir (agents/coder.py `_run_trailing_gates_if_build_complete`),
    written before QA ever runs by a different code path than the one asking
    to approve -- so it cannot be satisfied by an agent simply asserting
    `tests_passed`, the thing #1396 already proved cannot be trusted alone.

    #1545: the marker alone used to be enough, but it is persistent -- a
    worktree copy or a web-sync republish can carry a PRIOR build's passing
    marker into this one. `trailing_gate_evidence` now only returns evidence
    still bound to `project_dir`'s current git HEAD, so a marker recorded for
    a different tree reads as no evidence, not a stale pass.

    #1546+1: a STALE marker (one that exists but no longer matches HEAD) gets
    one re-run attempt here via `_refresh_stale_gate_evidence` before this
    refuses -- see that helper for what it does and does not retry. This
    closes the dead end where a trivial post-gate commit (the QA agent's own
    flake.nix edit, a fixer's re-commit) left a build permanently unable to
    reach a real verdict: nothing ever re-ran the gate, so the build sat in
    human_review having genuinely passed a run no marker could still prove.
    """
    unbuilt = _nothing_was_built(project_dir)
    if unbuilt:
        return (
            f"Refusing to approve: nothing was built. {unbuilt} Approving "
            "here would record a passing QA sign-off, and test results, for "
            "code that does not exist (#1396). Implement the change first; "
            "if the task genuinely requires no code change, say so rather "
            "than signing off."
        )

    missing = _missing_contract_exports(spec_dir, project_dir)
    if missing:
        return (
            "Refusing to approve: the spec enumerates an API this build "
            f"does not define — {', '.join(missing)}. The card named those "
            "so dependent cards could import them; with different names "
            "each one re-implements the whole thing instead (#1421). "
            "Export the names the spec asked for, or change the spec if "
            "they are wrong — do not sign off on an equivalent API under "
            "other names."
        )

    gate_evidence = trailing_gate_evidence(spec_dir, project_dir)
    if not evidence_shows_an_executed_gate(gate_evidence):
        # #1546+1: a marker that went stale because a commit landed AFTER the
        # authoritative gate run (a QA-fixer commit, the QA agent's own
        # flake.nix touch-up) is not the same failure as "no gate ever ran" --
        # re-running once, bound to the tree that would actually be approved,
        # is cheap relative to blocking every such build on a human. See
        # `_refresh_stale_gate_evidence` for why an ABSENT marker is not
        # retried here.
        gate_evidence = (
            await _refresh_stale_gate_evidence(spec_dir, project_dir) or gate_evidence
        )
    if not evidence_shows_an_executed_gate(gate_evidence):
        why = gate_evidence or "the gate step never ran for this build"
        return (
            f"Refusing to approve: no verification command is recorded as "
            f"having run ({why}). An APPROVED sign-off must be backed by an "
            "executed gate, not an agent's own reading of the code (#1496). "
            "If the toolchain is genuinely unavailable here, say so and "
            "leave this build unapproved -- do not record a pass for a "
            "suite that never ran."
        )
    if gate_outcomes_include_a_failure(gate_evidence):
        return (
            "Refusing to approve: the recorded verification gates did not "
            f"all pass ({gate_evidence}). A failing gate is evidence the "
            "build does not work, not something QA can sign off over "
            "(#1496). See GATE_FAILURES.md and fix the failure before "
            "approving."
        )

    return None


def create_qa_tools(
    spec_dir: Path | Callable[[], Path],
    project_dir: Path | Callable[[], Path],
) -> list:
    """
    Create QA management tools.

    Accepts either a fixed Path or a callable returning Path (Issue #10).

    Args:
        spec_dir: Path or Callable[[], Path] to the spec directory
        project_dir: Path or Callable[[], Path] to the project root

    Returns:
        List of QA tool functions
    """
    if not SDK_TOOLS_AVAILABLE:
        return []

    get_spec_dir: Callable[[], Path] = (
        spec_dir if callable(spec_dir) else (lambda p=spec_dir: p)
    )

    # A plain def rather than the `lambda p=project_dir: p` used just above:
    # mypy cannot infer that lambda's type and the cq ratchet counts net-new
    # errors per changed file, so copying the older idiom would have failed the
    # gate for a fresh line.
    def get_project_dir() -> Path:
        return project_dir() if callable(project_dir) else project_dir

    tools = []

    # -------------------------------------------------------------------------
    # Tool: update_qa_status
    # -------------------------------------------------------------------------
    @tool(
        "update_qa_status",
        "Update the QA sign-off status in implementation_plan.json. Use after QA review.",
        {"status": str, "issues": str, "tests_passed": str},
    )
    async def update_qa_status(args: dict[str, Any]) -> dict[str, Any]:
        """Update QA status in the implementation plan."""
        status = args["status"]
        issues_str = args.get("issues", "[]")
        tests_str = args.get("tests_passed", "{}")

        valid_statuses = [
            "pending",
            "in_review",
            "approved",
            "rejected",
            "fixes_applied",
        ]
        if status not in valid_statuses:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: Invalid QA status '{status}'. Must be one of: {valid_statuses}",
                    }
                ]
            }

        plan_file = get_spec_dir() / "implementation_plan.json"
        if not plan_file.exists():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: implementation_plan.json not found",
                    }
                ]
            }

        try:
            # Parse issues and tests
            try:
                issues = json.loads(issues_str) if issues_str else []
            except json.JSONDecodeError:
                issues = [{"description": issues_str}] if issues_str else []

            try:
                tests_passed = json.loads(tests_str) if tests_str else {}
            except json.JSONDecodeError:
                tests_passed = {}

            with open(plan_file) as f:
                plan = json.load(f)

            # Get current QA session number
            current_qa = plan.get("qa_signoff", {})
            qa_session = current_qa.get("qa_session", 0)
            if status in ["in_review", "rejected"]:
                qa_session += 1

            # An "approved" write is refused until every pre-approval
            # guard is satisfied -- see `_approval_refusal_reason` for what
            # each one catches and why (#1396, #1421, #1496). Pulled into one
            # helper (rather than inlined here) so this function's own
            # branch/return count stays readable -- the ratchet caught the
            # #1496 guard pushing update_qa_status over the complexity caps.
            if status == "approved":
                refusal = await _approval_refusal_reason(
                    get_spec_dir(), get_project_dir()
                )
                if refusal:
                    return {"content": [{"type": "text", "text": refusal}]}

            plan["qa_signoff"] = {
                "status": status,
                "qa_session": qa_session,
                "issues_found": issues,
                "tests_passed": tests_passed,
                "timestamp": datetime.now(UTC).isoformat(),
                "ready_for_qa_revalidation": status == "fixes_applied",
            }

            # Update plan status to match QA result
            # This ensures the UI shows the correct column after QA
            if status == "approved":
                plan["status"] = "human_review"
                plan["planStatus"] = "review"
                plan["reviewReason"] = "completed"
            elif status == "rejected":
                plan["status"] = "human_review"
                plan["planStatus"] = "review"
                plan["reviewReason"] = "qa_issues"

            plan["last_updated"] = datetime.now(UTC).isoformat()

            with open(plan_file, "w") as f:
                json.dump(plan, f, indent=2)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Updated QA status to '{status}' (session {qa_session})",
                    }
                ]
            }

        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error updating QA status: {e}"}]
            }

    tools.append(update_qa_status)

    return tools
