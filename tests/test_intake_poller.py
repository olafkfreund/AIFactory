"""Exactly-once tests for the RFC-0011 intake poller (#636).

The poller's two-guard idempotency is the core deliverable, so these tests are
the heart of the issue. They use a fake provider/router (no network) and a real
SQLite processed-store on a temp DB to prove exactly-once dispatch over:

  * two consecutive poll passes,
  * a simulated process restart (the DB persists, the in-memory state does not),
  * a deleted DB (the applied factory:queued label still blocks re-dispatch).

Plus tier routing (low/medium -> /from-issue, hard -> PFactory), terminal vs
transient failure handling, and highest-wins / migration overrides.
"""

from __future__ import annotations

from functools import partial

import pytest
from intake import processed_store
from intake.poller import (
    FAILED_LABEL,
    QUEUED_LABEL,
    IntakeIssue,
    PollerDeps,
    RepoConfig,
    TerminalIntakeError,
    poll_once,
)


class FakeWorld:
    """A fake provider + downstream: issues with mutable labels, recorded routes."""

    def __init__(self, db_path):
        self.issues: dict[int, IntakeIssue] = {}
        self.routes: list[tuple[int, str]] = []  # (number, "low-med"|"hard")
        self.comments: list[tuple[int, str]] = []
        self.db_path = db_path
        self.fail_terminal: set[int] = set()
        self.fail_transient: set[int] = set()

    def add(self, number, labels):
        self.issues[number] = IntakeIssue(number=number, labels=list(labels))

    # --- collaborators wired into PollerDeps ---
    def fetch(self, cfg):
        # Mimic the provider query: only factory:* labels, exclude factory:queued.
        out = []
        for iss in self.issues.values():
            lower = {label.lower() for label in iss.labels}
            if QUEUED_LABEL in lower:
                continue
            if any(label.lower().startswith("factory:") for label in iss.labels):
                out.append(IntakeIssue(iss.number, list(iss.labels)))
        return out

    def apply_label(self, cfg, number, label):
        if label not in self.issues[number].labels:
            self.issues[number].labels.append(label)

    def comment(self, cfg, number, body):
        self.comments.append((number, body))

    def route_low_medium(self, cfg, issue, tier):
        if issue.number in self.fail_terminal:
            raise TerminalIntakeError("boom")
        if issue.number in self.fail_transient:
            raise RuntimeError("temporary")
        self.routes.append((issue.number, "low-med"))

    def route_hard(self, cfg, issue, tier):
        if issue.number in self.fail_terminal:
            raise TerminalIntakeError("boom")
        if issue.number in self.fail_transient:
            raise RuntimeError("temporary")
        self.routes.append((issue.number, "hard"))

    def deps(self, reclaim_after_s: float = 600.0):
        return PollerDeps(
            fetch_issues=self.fetch,
            apply_label=self.apply_label,
            comment=self.comment,
            route_low_medium=self.route_low_medium,
            route_hard=self.route_hard,
            mark_processed=partial(
                processed_store.mark_processed,
                path=self.db_path,
                reclaim_after_s=reclaim_after_s,
            ),
            unmark_processed=partial(
                processed_store.unmark_processed, path=self.db_path
            ),
            confirm_processed=partial(
                processed_store.confirm_processed, path=self.db_path
            ),
        )


CFG = RepoConfig(provider="github", repo="o/r", project_id="p")


@pytest.fixture
def world(tmp_path):
    return FakeWorld(tmp_path / "intake.db")


# ── exactly-once: two loop passes ──────────────────────────────────────────


def test_exactly_once_over_two_passes(world):
    world.add(1, ["factory:low"])
    deps = world.deps()

    c1 = poll_once([CFG], deps)
    assert c1["routed"] == 1
    assert (1, "low-med") in world.routes
    assert QUEUED_LABEL in world.issues[1].labels

    # Second pass: the issue is now factory:queued -> excluded by fetch entirely.
    c2 = poll_once([CFG], deps)
    assert c2["routed"] == 0
    assert world.routes.count((1, "low-med")) == 1  # routed exactly once


# ── exactly-once: simulated restart (DB persists, memory does not) ──────────


def test_exactly_once_across_restart(world):
    world.add(2, ["factory:medium"])
    poll_once([CFG], world.deps())
    assert world.routes.count((2, "low-med")) == 1

    # Simulate a restart: brand-new deps (fresh in-memory state) but the SAME DB
    # file. The factory:queued label is also still on the issue.
    fresh_deps = world.deps()
    poll_once([CFG], fresh_deps)
    assert world.routes.count((2, "low-med")) == 1  # still exactly once


# ── exactly-once: DB deleted, factory:queued label still blocks ────────────


def test_label_guard_blocks_when_db_deleted(world):
    world.add(3, ["factory:low"])
    poll_once([CFG], world.deps())
    assert world.routes.count((3, "low-med")) == 1
    assert QUEUED_LABEL in world.issues[3].labels

    # Nuke the durable store entirely — guard 1 is gone.
    world.db_path.unlink()
    assert processed_store.processed_count(path=world.db_path) == 0

    # Guard 2 (the label) still excludes the issue at fetch time -> no re-route.
    poll_once([CFG], world.deps())
    assert world.routes.count((3, "low-med")) == 1


# ── tier routing ───────────────────────────────────────────────────────────


def test_hard_routes_to_pfactory(world):
    world.add(4, ["factory:hard"])
    poll_once([CFG], world.deps())
    assert (4, "hard") in world.routes


def test_highest_wins_routes_hard(world):
    world.add(5, ["factory:low", "factory:hard"])
    poll_once([CFG], world.deps())
    assert (5, "hard") in world.routes


def test_migration_forces_hard(world):
    world.add(6, ["factory:low"])
    cfg = RepoConfig("github", "o/r", "p", change_mode="migration")
    poll_once([cfg], world.deps())
    assert (6, "hard") in world.routes


def test_no_tier_label_skipped(world):
    world.add(7, ["bug"])  # fetched only if it has factory:* — it doesn't
    c = poll_once([CFG], world.deps())
    assert c["routed"] == 0
    assert world.routes == []


# ── failure handling ───────────────────────────────────────────────────────


def test_terminal_failure_labels_and_comments(world):
    world.add(8, ["factory:low"])
    world.fail_terminal.add(8)
    c = poll_once([CFG], world.deps())
    assert c["failed"] == 1
    assert FAILED_LABEL in world.issues[8].labels
    assert QUEUED_LABEL not in world.issues[8].labels
    assert any(n == 8 for n, _ in world.comments)


def test_transient_failure_retries_next_tick(world):
    world.add(9, ["factory:medium"])
    world.fail_transient.add(9)
    c1 = poll_once([CFG], world.deps())
    assert c1["transient"] == 1
    assert QUEUED_LABEL not in world.issues[9].labels
    assert FAILED_LABEL not in world.issues[9].labels

    # The claim was rolled back by unmark_processed on the transient failure, so
    # the issue (still unlabelled) is re-fetched and routed on the next tick.
    world.fail_transient.discard(9)
    c2 = poll_once([CFG], world.deps())
    assert c2["routed"] == 1
    assert world.routes.count((9, "low-med")) == 1


# ── robustness ─────────────────────────────────────────────────────────────


def test_fetch_error_one_repo_does_not_kill_pass(world):
    bad = RepoConfig("github", "bad/repo", "p")
    world.add(10, ["factory:low"])

    def fetch(cfg):
        if cfg.repo == "bad/repo":
            raise RuntimeError("provider down")
        return world.fetch(cfg)

    deps = world.deps()
    deps.fetch_issues = fetch
    c = poll_once([bad, CFG], deps)
    assert c["fetch-errors"] == 1
    assert c["routed"] == 1  # the good repo still processed


# ── #870: a crashed route (claimed, never confirmed) is reclaimed ───────────


def test_confirmed_after_successful_route(world):
    """A normal route confirms the claim, so it is never reclaimed later."""
    world.add(20, ["factory:low"])
    poll_once([CFG], world.deps(reclaim_after_s=0))  # even a 0s window...
    poll_once([CFG], world.deps(reclaim_after_s=0))  # ...must not re-route
    assert world.routes.count((20, "low-med")) == 1


def test_stranded_claim_is_reclaimed_and_rerouted(world):
    """The #870 bug: a pod died between claiming an issue and routing it, so the
    claim sits unconfirmed with no build. Once stale it must be reclaimed and the
    issue routed, not stranded forever."""
    world.add(21, ["factory:low"])
    # Simulate the crash: claim the issue directly (unconfirmed) and DON'T route.
    assert processed_store.mark_processed("o/r", 21, path=world.db_path) is True
    assert world.routes == []  # nothing was routed — the pod died here
    # The issue still lacks factory:queued, so it is fetched again. With the
    # claim now stale (reclaim_after_s=0), the next poll reclaims + routes it.
    poll_once([CFG], world.deps(reclaim_after_s=0))
    assert (21, "low-med") in world.routes, (
        "a stranded claim must be reclaimed + routed"
    )
    assert QUEUED_LABEL in world.issues[21].labels

    # And now that it is confirmed, it is not routed a second time.
    poll_once([CFG], world.deps(reclaim_after_s=0))
    assert world.routes.count((21, "low-med")) == 1
