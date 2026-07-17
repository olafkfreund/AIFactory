"""Tests for the intake-poller lifespan service config (RFC-0011 #636).

Covers env-gating and AIFACTORY_INTAKE_REPOS parsing — the pure config surface.
The agent SDK is pre-mocked by conftest; the route/provider seams are imported
lazily inside the loop, so importing the service module is cheap.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from pfactory.tiers import Tier  # noqa: E402
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


def test_load_repo_configs_base_branch(monkeypatch):
    """base_branch is parsed per repo entry and defaults to None (repos whose
    integration branch is not the default branch, e.g. the fleet's dev)."""
    monkeypatch.setenv(
        "AIFACTORY_INTAKE_REPOS",
        '[{"repo":"o/r","project_id":"p","base_branch":"dev"},'
        '{"repo":"o/s","project_id":"q"}]',
    )
    cfgs = ip.load_repo_configs()
    assert cfgs[0].base_branch == "dev"
    assert cfgs[1].base_branch is None


def test_route_low_medium_payload_carries_base_branch(monkeypatch):
    """The from-issue payload threads the repo's integration branch so the
    worktree AND the eventual auto-PR target it instead of main."""
    monkeypatch.setenv("AIFACTORY_URL", "https://aifactory.example")
    seen = {}

    def fake_post(url, payload, timeout=10.0, token=None):
        seen.update(url=url, payload=payload)

    monkeypatch.setattr(ip, "_post_json", fake_post)
    cfg = ip.RepoConfig(
        provider="github", repo="o/r", project_id="p", base_branch="dev"
    )
    issue = ip.IntakeIssue(number=7, labels=["factory:low"])
    ip._route_low_medium(cfg, issue, Tier.LOW)

    assert seen["url"] == "https://aifactory.example/api/tasks/from-issue"
    assert seen["payload"]["base_branch"] == "dev"
    assert seen["payload"]["issue_number"] == 7


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


# --- #874: hard tier routes to PFactory when configured; refuses loudly if not ---


def _hard_args():
    """(cfg, issue, tier) for a factory:hard issue, as poll_once would pass them."""
    cfg = ip.RepoConfig(
        provider="github",
        repo="olafkfreund/AIFactory",
        project_id="aifactory",
        change_mode="existing",
    )
    issue = ip.IntakeIssue(
        number=874,
        labels=["factory:hard"],
        title="Refund flow",
        body="## Acceptance Criteria\n- A finance user can issue a refund\n",
    )
    return cfg, issue, Tier.HARD


def test_route_hard_refuses_loudly_and_actionably(monkeypatch):
    """With no PFACTORY_INGEST_URL a hard-tier issue must refuse with a
    maintainer-actionable message (which becomes the issue comment), not a silent
    drop — an unconfigured deployment degrades honestly (#843)."""
    monkeypatch.delenv("PFACTORY_INGEST_URL", raising=False)
    monkeypatch.setattr(
        ip, "_post_json", lambda *a, **k: pytest.fail("must not POST when unconfigured")
    )

    with pytest.raises(ip.TerminalIntakeError) as exc:
        ip._route_hard(*_hard_args())

    msg = str(exc.value).lower()
    assert "manually" in msg and "hard" in msg, (
        f"refusal must tell a maintainer what to do; got: {exc.value}"
    )


def test_route_hard_posts_the_pfactory_ingest_contract(monkeypatch):
    """The payload IS the contract PFactory's /from-issue endpoint accepts —
    notably issue_number, the RFC-0001 correlation key it plans against."""
    monkeypatch.setenv(
        "PFACTORY_INGEST_URL", "https://pfactory.example/api/plan/sessions/from-issue/"
    )
    monkeypatch.setenv("PFACTORY_TOKEN", "pf-tok")
    seen = {}

    def fake_post(url, payload, timeout=10.0, token=None):
        seen.update(url=url, payload=payload, token=token)

    monkeypatch.setattr(ip, "_post_json", fake_post)
    ip._route_hard(*_hard_args())

    # Trailing slash stripped, so the configured URL is used verbatim.
    assert seen["url"] == "https://pfactory.example/api/plan/sessions/from-issue"
    assert seen["payload"] == {
        "repo": "olafkfreund/AIFactory",
        "provider": "github",
        "issue_number": 874,
        "title": "Refund flow",
        "body": "## Acceptance Criteria\n- A finance user can issue a refund\n",
        "labels": ["factory:hard"],
        "autonomy_tier": "hard",
        "change_mode": "existing",
    }
    # A sibling's API: authenticated, and never with AIFactory's own token.
    assert seen["token"] == "pf-tok"


def test_route_hard_authenticates_to_a_sibling_not_with_our_own_token(monkeypatch):
    """PFactory is a sibling service, so AIFACTORY_TOKEN cannot authenticate to
    it (#847's reasoning); prefer PFACTORY_TOKEN, else the shared APP_API_TOKEN."""
    monkeypatch.delenv("PFACTORY_TOKEN", raising=False)
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.setenv("AIFACTORY_TOKEN", "aif-tok-must-not-be-used")
    assert ip._pfactory_token() is None

    monkeypatch.setenv("APP_API_TOKEN", "app-tok")
    assert ip._pfactory_token() == "app-tok", "falls back to the shared token"

    monkeypatch.setenv("PFACTORY_TOKEN", "pf-tok")
    assert ip._pfactory_token() == "pf-tok", "explicit PFACTORY_TOKEN wins"


def test_route_hard_4xx_surfaces_pfactorys_reason(monkeypatch):
    """A rejected issue must comment back WHY (e.g. no acceptance criteria), not
    a bare "HTTP 400" the human who filed it cannot act on."""
    import urllib.error

    monkeypatch.setenv("PFACTORY_INGEST_URL", "https://pfactory.example/ingest")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://pfactory.example/ingest",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"detail":"no acceptance criteria found"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(ip.TerminalIntakeError) as exc:
        ip._route_hard(*_hard_args())

    assert "no acceptance criteria found" in str(exc.value)
    assert "400" in str(exc.value)


def test_post_json_5xx_stays_transient(monkeypatch):
    """A 5xx is the server's problem and may fix itself — must NOT go terminal
    (which would comment + give up on the issue)."""
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://x.example/i", 503, "Service Unavailable", {}, io.BytesIO(b"nope")
        )

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(RuntimeError) as exc:
        ip._post_json("https://x.example/i", {"a": 1})
    assert not isinstance(exc.value, ip.TerminalIntakeError)


def test_http_detail_never_masks_the_status():
    """An unreadable/empty body must degrade to "" rather than raise — the
    status code is the load-bearing part."""

    class _Unreadable:
        def read(self):
            raise OSError("connection reset")

    assert ip._http_detail(_Unreadable()) == ""

    class _Empty:
        def read(self):
            return b"   "

    assert ip._http_detail(_Empty()) == ""

    class _Plain:
        def read(self):
            return b"upstream exploded"

    assert ip._http_detail(_Plain()) == ": upstream exploded"


# --- #847: the API token never falls back to a GitHub PAT (guaranteed 401) ---


def test_api_token_ignores_github_pat(monkeypatch):
    """_post_json calls our OWN API; a GH PAT can only 401 there, so GH_TOKEN
    must not be used as a fallback (#847)."""
    monkeypatch.delenv("AIFACTORY_TOKEN", raising=False)
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_should_be_ignored")
    assert ip._api_token() is None, "GH_TOKEN must not authenticate the AIFactory API"

    monkeypatch.setenv("APP_API_TOKEN", "app-tok")
    assert ip._api_token() == "app-tok", "falls back to the API's own token"

    monkeypatch.setenv("AIFACTORY_TOKEN", "aif-tok")
    assert ip._api_token() == "aif-tok", "explicit AIFACTORY_TOKEN wins"
