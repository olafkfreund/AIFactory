"""Tests for the intake-poller lifespan service config (RFC-0011 #636).

Covers env-gating and AIFACTORY_INTAKE_REPOS parsing — the pure config surface.
The agent SDK is pre-mocked by conftest; the route/provider seams are imported
lazily inside the loop, so importing the service module is cheap.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services import intake_poller as ip  # noqa: E402


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AIFACTORY_INTAKE_POLLER", raising=False)
    assert ip.poller_enabled() is False


def test_enabled_truthy(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("AIFACTORY_INTAKE_POLLER", val)
        assert ip.poller_enabled() is True


def test_interval_default_and_floor(monkeypatch):
    monkeypatch.delenv("AIFACTORY_INTAKE_INTERVAL_S", raising=False)
    assert ip.interval_s() == 30.0
    monkeypatch.setenv("AIFACTORY_INTAKE_INTERVAL_S", "1")
    assert ip.interval_s() == 5.0  # floored at 5s
    monkeypatch.setenv("AIFACTORY_INTAKE_INTERVAL_S", "bogus")
    assert ip.interval_s() == 30.0


def test_load_repo_configs_valid(monkeypatch):
    monkeypatch.setenv(
        "AIFACTORY_INTAKE_REPOS",
        '[{"provider":"gitlab","repo":"o/r","project_id":"p","change_mode":"migration"}]',
    )
    cfgs = ip.load_repo_configs()
    assert len(cfgs) == 1
    assert cfgs[0].provider == "gitlab"
    assert cfgs[0].repo == "o/r"
    assert cfgs[0].project_id == "p"
    assert cfgs[0].change_mode == "migration"


def test_load_repo_configs_defaults_provider_github(monkeypatch):
    monkeypatch.setenv("AIFACTORY_INTAKE_REPOS", '[{"repo":"o/r","project_id":"p"}]')
    cfgs = ip.load_repo_configs()
    assert cfgs[0].provider == "github"


def test_load_repo_configs_skips_incomplete(monkeypatch):
    monkeypatch.setenv(
        "AIFACTORY_INTAKE_REPOS",
        '[{"repo":"o/r"},{"project_id":"p"},{"repo":"x","project_id":"y"}]',
    )
    cfgs = ip.load_repo_configs()
    assert len(cfgs) == 1
    assert cfgs[0].repo == "x"


def test_load_repo_configs_bad_json(monkeypatch):
    monkeypatch.setenv("AIFACTORY_INTAKE_REPOS", "not json")
    assert ip.load_repo_configs() == []


def test_load_repo_configs_empty(monkeypatch):
    monkeypatch.delenv("AIFACTORY_INTAKE_REPOS", raising=False)
    assert ip.load_repo_configs() == []


# --- #868: poller_loop liveness (heartbeat) + wedge-safety (per-poll timeout) ---


@pytest.mark.asyncio
async def test_idle_poll_logs_heartbeat(monkeypatch, caplog):
    """A healthy poll that routes nothing (all skipped-queued) must still emit an
    idle heartbeat — otherwise 'started but never polls' is invisible (#868)."""
    stop = asyncio.Event()

    def fake_poll_once(repos, deps):
        stop.set()  # end the loop after this one tick
        return {"routed": 0, "failed": 0, "skipped-queued": 3}

    monkeypatch.setattr(ip, "poll_once", fake_poll_once)
    with caplog.at_level(logging.INFO):
        # interval=300 -> heartbeat_every == 1, so tick 1 heartbeats
        await ip.poller_loop(interval=300, stop=stop, deps=object(), repos=[object()])

    assert any("heartbeat" in r.getMessage() for r in caplog.records), (
        "an idle poll must log a heartbeat so the poller's liveness is visible"
    )


@pytest.mark.asyncio
async def test_hung_poll_is_abandoned_not_wedged(monkeypatch, caplog):
    """A poll that blocks past the timeout is abandoned and the loop continues —
    a hung provider/network call can no longer wedge intake forever (#868)."""
    stop = asyncio.Event()
    calls = {"n": 0}

    def maybe_hang(repos, deps):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.5)  # first tick blocks past poll_timeout -> abandoned
        else:
            stop.set()  # a later tick still runs -> loop was NOT wedged
        return {"routed": 0, "failed": 0}

    monkeypatch.setattr(ip, "poll_once", maybe_hang)
    with caplog.at_level(logging.WARNING):
        await ip.poller_loop(
            interval=0.05, stop=stop, deps=object(), repos=[object()], poll_timeout=0.1
        )

    assert any("abandoned this tick" in r.getMessage() for r in caplog.records), (
        "a hung poll must log the timeout warning"
    )
    assert calls["n"] >= 2, "the loop must keep ticking after abandoning a hung poll"


# --- #861: intake creates its own missing factory:* label, then applies it ---


def test_apply_label_creates_missing_label_then_retries(monkeypatch):
    """A repo not yet seeded with factory:queued must not leave routed issues
    stuck at factory:low — the label is created on demand and re-applied."""
    events = []

    class FakeProvider:
        def __init__(self):
            self.applies = 0

        async def apply_labels(self, number, labels):
            self.applies += 1
            if self.applies == 1:  # gh: label doesn't exist yet
                raise RuntimeError("'factory:queued' not found")
            events.append(("apply", number, tuple(labels)))

        async def create_label(self, label):
            events.append(("create", label.name, label.color))

    monkeypatch.setattr(ip, "_provider_for", lambda cfg: FakeProvider())
    ip._apply_label(object(), 42, "factory:queued")

    assert ("create", "factory:queued", "0e8a16") in events, (
        "missing label must be created"
    )
    assert ("apply", 42, ("factory:queued",)) in events, "then applied on retry"


def test_apply_label_does_not_create_when_present(monkeypatch):
    """The common path (label already exists) applies once, never creates."""
    events = []

    class FakeProvider:
        async def apply_labels(self, number, labels):
            events.append("apply")

        async def create_label(self, label):
            events.append("create")

    monkeypatch.setattr(ip, "_provider_for", lambda cfg: FakeProvider())
    ip._apply_label(object(), 1, "factory:queued")

    assert events == ["apply"], "no label creation when the apply succeeds"


# --- #843: hard tier is not auto-routed yet -> loud, actionable refusal ---


def test_route_hard_refuses_loudly_and_actionably(monkeypatch):
    """Until a PFactory ingest endpoint exists, a hard-tier issue must refuse
    with a maintainer-actionable message (which becomes the issue comment), not
    a silent mis-POST or an internal env-var error."""
    monkeypatch.delenv("PFACTORY_INGEST_URL", raising=False)
    with pytest.raises(ip.TerminalIntakeError) as exc:
        ip._route_hard(object(), object(), object())
    msg = str(exc.value).lower()
    assert "manually" in msg and "hard" in msg, (
        f"refusal must tell a maintainer what to do; got: {exc.value}"
    )
