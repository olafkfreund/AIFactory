"""Provider-agnostic label-driven intake poller — pure loop logic (RFC-0011 #636).

This module holds the *decision* logic with no FastAPI / asyncio / network
coupling, so the exactly-once guarantees can be unit-tested deterministically.
The async lifespan wrapper lives in ``server/services/intake_poller.py``.

For each configured repo the poller:
  1. Fetches open issues carrying any ``factory:<tier>`` label, **excluding**
     those already labelled ``factory:queued`` (the fetch-side guard).
  2. For each candidate, applies **two-guard idempotency**:
       a. the durable processed-store (``mark_processed`` INSERT-OR-IGNORE), and
       b. the ``factory:queued`` label (re-checked on the fetched issue).
     Both must be clear for the issue to be routed; routing happens once.
  3. Routes by tier: low/medium -> AIFactory ``/api/tasks/from-issue``;
     hard -> PFactory ingest (decompose -> human approve -> emit -> AIFactory).
  4. On success applies ``factory:queued``. On a *terminal* failure applies
     ``factory:failed`` + a comment (never silently drops). Transient failures
     are left for the next tick (no label, processed-row rolled back).

All side-effecting collaborators are passed in via :class:`PollerDeps`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pfactory.tiers import Tier, classify_tier, tier_for

logger = logging.getLogger(__name__)

__all__ = [
    "FAILED_LABEL",
    "QUEUED_LABEL",
    "TIER_LABELS",
    "IntakeIssue",
    "PollerDeps",
    "RepoConfig",
    "TerminalIntakeError",
    "poll_once",
    "process_issue",
]

QUEUED_LABEL = "factory:queued"
FAILED_LABEL = "factory:failed"
TIER_LABELS = ("factory:low", "factory:medium", "factory:hard")


class TerminalIntakeError(Exception):
    """A non-retryable routing failure — apply ``factory:failed`` + comment."""


@dataclass
class IntakeIssue:
    """Minimal provider-agnostic issue view the poller reasons over."""

    number: int
    labels: list[str]
    title: str = ""
    body: str = ""
    url: str = ""


@dataclass
class RepoConfig:
    """One repo to poll: provider hint, repo slug, and the AIFactory project."""

    provider: str
    repo: str
    project_id: str
    change_mode: str | None = None  # optional per-repo override
    # Integration branch the build's worktree AND its eventual PR should target
    # (e.g. "dev" for repos that do not integrate via their default branch).
    base_branch: str | None = None


@dataclass
class PollerDeps:
    """Injected side-effecting collaborators (all simple callables for tests).

    fetch_issues(cfg) -> sequence of IntakeIssue (already filtered to factory:*
        labels and excluding factory:queued by the provider query).
    apply_label(cfg, number, label) -> None
    comment(cfg, number, body) -> None
    route_low_medium(cfg, issue, tier) -> None   (raise TerminalIntakeError on
        a permanent failure; any other exception is treated as transient)
    route_hard(cfg, issue, tier) -> None
    mark_processed(repo, number, tier, routed_to) -> bool   (True == newly claimed)
    unmark_processed(repo, number) -> None   (best-effort rollback on transient fail)
    """

    fetch_issues: Callable[[RepoConfig], Sequence[IntakeIssue]]
    apply_label: Callable[[RepoConfig, int, str], None]
    comment: Callable[[RepoConfig, int, str], None]
    route_low_medium: Callable[[RepoConfig, IntakeIssue, Tier], None]
    route_hard: Callable[[RepoConfig, IntakeIssue, Tier], None]
    mark_processed: Callable[..., bool]
    unmark_processed: Callable[[str, int], None] = field(default=lambda repo, n: None)
    # #870: confirm a claim once the issue is actually routed, so a crash between
    # claiming and routing leaves an UNCONFIRMED claim the next tick can reclaim.
    confirm_processed: Callable[[str, int], None] = field(default=lambda *_: None)
    # #941 self-heal: an issue can end up factory:queued (routed) yet never
    # actually build (a dropped dispatch, a pod roll). These re-open it. All
    # optional — unset, the queued branch stays a plain skip (back-compat).
    #   build_exists(cfg, issue_number) -> bool   True == a real build exists.
    #   claimed_at(repo, issue_number)  -> float|None   epoch it was queued.
    #   requeue(cfg, issue_number)      -> None   clear the guard so it re-routes.
    build_exists: Callable[[RepoConfig, int], bool] | None = None
    claimed_at: Callable[[str, int], float | None] | None = None
    requeue: Callable[[RepoConfig, int], None] | None = None
    # Grace before a build-less queued issue is treated as orphaned + re-opened.
    requeue_after_s: float = 600.0


def _has_tier_label(labels: Sequence[str]) -> bool:
    return classify_tier(list(labels)) is not None


def process_issue(cfg: RepoConfig, issue: IntakeIssue, deps: PollerDeps) -> str:
    """Process a single fetched issue. Returns an outcome string.

    Outcomes: ``"routed"`` (dispatched + queued), ``"skipped-queued"`` (already
    queued by label or processed-store), ``"skipped-no-tier"``, ``"failed"``
    (terminal — failed-labelled + commented), ``"transient"`` (left for retry).
    """
    labels_lower = {label.lower() for label in issue.labels}

    # Guard 2 (label): even with a wiped DB, an already-queued issue is skipped
    # from routing — but first give it to the self-heal, which re-opens it only
    # if it was queued yet never built (#941).
    if QUEUED_LABEL in labels_lower:
        return _maybe_self_heal(cfg, issue, deps)

    if not _has_tier_label(issue.labels):
        return "skipped-no-tier"

    # Resolve the tier (highest-wins; migration forces hard).
    labelled = classify_tier(issue.labels)

    class _Carrier:
        tier = labelled

    tier = tier_for(_Carrier(), change_mode=cfg.change_mode)

    routed_to = "pfactory" if tier is Tier.HARD else "aifactory"

    # Guard 1 (durable store): atomically claim the issue. Only the winner routes.
    claimed = deps.mark_processed(
        cfg.repo, issue.number, tier=tier.value, routed_to=routed_to
    )
    if not claimed:
        return "skipped-queued"

    # Route. Terminal failure => failed label + comment (never drop). Transient
    # failure => roll back the claim so the next tick retries, leave no label.
    try:
        if tier is Tier.HARD:
            deps.route_hard(cfg, issue, tier)
        else:
            deps.route_low_medium(cfg, issue, tier)
    except TerminalIntakeError as exc:
        logger.error(
            "intake terminal failure on %s#%s: %s", cfg.repo, issue.number, exc
        )
        _safe(deps.apply_label, cfg, issue.number, FAILED_LABEL)
        _safe(
            deps.comment,
            cfg,
            issue.number,
            f"AIFactory intake failed (terminal): {exc}",
        )
        return "failed"
    except Exception as exc:  # noqa: BLE001 — transient: retry next tick
        logger.warning(
            "intake transient failure on %s#%s (will retry): %s",
            cfg.repo,
            issue.number,
            exc,
        )
        _safe(deps.unmark_processed, cfg.repo, issue.number)
        return "transient"

    # Success: confirm the claim FIRST (#870) — the route completed, so this
    # issue must never be reclaimed/re-routed even if the label write below (or
    # the pod) fails a moment later. Then apply factory:queued (guard-2 marker
    # for all future ticks).
    _safe(deps.confirm_processed, cfg.repo, issue.number)
    _safe(deps.apply_label, cfg, issue.number, QUEUED_LABEL)
    logger.info(
        "intake routed %s#%s as %s -> %s", cfg.repo, issue.number, tier.value, routed_to
    )
    return "routed"


def _maybe_self_heal(cfg: RepoConfig, issue: IntakeIssue, deps: PollerDeps) -> str:
    """Re-open a ``factory:queued`` issue that never produced a build (#941).

    Routing applies ``factory:queued``, which then hides the issue from every
    future poll (guard 2). If the build was never actually created (a dropped
    dispatch, a pod roll between route and build), that guard strands the issue
    forever. So for a queued issue with NO build past a grace, clear the guard +
    release the claim (``requeue``) so the next tick re-routes it — idempotency
    downstream (#878) means the re-route adopts any spec that did land, never a
    duplicate. Conservative by construction: an issue that HAS a build, is within
    grace, is hard-tier (its build lives in PFactory, invisible here), or whose
    state cannot be read is left untouched — never a double-dispatch.
    """
    if deps.build_exists is None or deps.requeue is None:
        return "skipped-queued"  # self-heal not wired

    # Only low/medium creates an AIFactory spec we can see; a hard issue is still
    # in PFactory planning, so absence of a spec here is not an orphan.
    labelled = classify_tier(issue.labels)
    if labelled is None:
        return "skipped-queued"

    class _Carrier:
        tier = labelled

    if tier_for(_Carrier(), change_mode=cfg.change_mode) is Tier.HARD:
        return "skipped-queued"

    try:
        if deps.build_exists(cfg, issue.number):
            return "skipped-queued"  # has a build — leave it alone
    except Exception:  # noqa: BLE001 — unreadable state must never re-dispatch
        logger.exception(
            "intake self-heal build-check failed on %s#%s", cfg.repo, issue.number
        )
        return "skipped-queued"

    claimed = deps.claimed_at(cfg.repo, issue.number) if deps.claimed_at else None
    if claimed is None:
        return "skipped-queued"  # no timestamp -> cannot age it safely

    import time

    age = time.time() - claimed
    if age < deps.requeue_after_s:
        return "skipped-queued"  # still within grace

    logger.warning(
        "intake self-heal: %s#%s is factory:queued with no build %.0fs after "
        "routing; clearing the guard to re-dispatch",
        cfg.repo,
        issue.number,
        age,
    )
    _safe(deps.requeue, cfg, issue.number)
    return "requeued"


def poll_once(repos: Sequence[RepoConfig], deps: PollerDeps) -> dict[str, int]:
    """Run one polling pass over all repos. Returns outcome counts. Never raises."""
    counts: dict[str, int] = {
        "routed": 0,
        "requeued": 0,
        "skipped-queued": 0,
        "skipped-no-tier": 0,
        "failed": 0,
        "transient": 0,
        "fetch-errors": 0,
    }
    for cfg in repos:
        try:
            issues = deps.fetch_issues(cfg)
        except Exception:  # noqa: BLE001 — one bad repo never kills the pass
            logger.exception("intake fetch failed for %s", cfg.repo)
            counts["fetch-errors"] += 1
            continue
        for issue in issues:
            try:
                outcome = process_issue(cfg, issue, deps)
            except Exception:  # noqa: BLE001 — defensive; one issue never kills the repo
                logger.exception(
                    "intake process crashed on %s#%s", cfg.repo, issue.number
                )
                outcome = "transient"
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _safe(fn: Callable[..., object], *args: object) -> None:
    """Call a best-effort side effect, swallowing errors (labels/comments)."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        logger.exception("intake side-effect failed (best-effort)")
