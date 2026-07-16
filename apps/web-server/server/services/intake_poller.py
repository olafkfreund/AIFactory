"""Lifespan wrapper for the RFC-0011 intake poller (#636).

Wires the pure loop logic in ``intake.poller`` to real collaborators (the
GitProvider abstraction for fetch/label/comment, HTTP for routing) and runs it
as a background asyncio task, modeled on ``services/outbox.py::relay_loop`` +
the web-server lifespan.

Env-gated and **off by default**:
  AIFACTORY_INTAKE_POLLER   "true" to enable (default off).
  AIFACTORY_INTAKE_REPOS    JSON list of
      {"provider","repo","project_id"[,"change_mode"]}.
  AIFACTORY_INTAKE_INTERVAL_S   poll interval seconds (default 30).
  AIFACTORY_URL                 base URL for the /api/tasks/from-issue route.
  PFACTORY_INGEST_URL           PFactory ingest endpoint for hard-tier routing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Backend seams (intake.poller, providers) — mirror execution.py's path shim.
_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from intake import processed_store  # noqa: E402
from intake.poller import (  # noqa: E402
    IntakeIssue,
    PollerDeps,
    RepoConfig,
    TerminalIntakeError,
    poll_once,
)
from pfactory.tiers import Tier  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "poller_enabled",
    "load_repo_configs",
    "interval_s",
    "build_deps",
    "poller_loop",
]


def poller_enabled() -> bool:
    """Whether the intake poller is turned on (default off)."""
    return (os.environ.get("AIFACTORY_INTAKE_POLLER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def interval_s() -> float:
    try:
        return max(5.0, float(os.environ.get("AIFACTORY_INTAKE_INTERVAL_S", "30")))
    except (TypeError, ValueError):
        return 30.0


def load_repo_configs() -> list[RepoConfig]:
    """Parse AIFACTORY_INTAKE_REPOS (JSON). Returns [] on any error."""
    raw = (os.environ.get("AIFACTORY_INTAKE_REPOS") or "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("AIFACTORY_INTAKE_REPOS is not valid JSON; poller idle")
        return []
    out: list[RepoConfig] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        repo = it.get("repo")
        project_id = it.get("project_id")
        if not repo or not project_id:
            continue
        out.append(
            RepoConfig(
                provider=str(it.get("provider", "github")),
                repo=str(repo),
                project_id=str(project_id),
                change_mode=it.get("change_mode"),
            )
        )
    return out


# --------------------------------------------------------------------------
# Real collaborators
# --------------------------------------------------------------------------


def _provider_for(cfg: RepoConfig):
    from ..routes.github import _get_project_provider

    return _get_project_provider(cfg.project_id)


def _run_async(coro):
    """Run a coroutine to completion from the (sync) poller thread."""
    import asyncio

    return asyncio.run(coro)


def _fetch_issues(cfg: RepoConfig) -> list[IntakeIssue]:
    """Fetch open factory:* issues excluding factory:queued via the provider."""
    from runners.github.providers.protocol import IssueFilters

    async def _go() -> list[IntakeIssue]:
        provider = _provider_for(cfg)
        # The provider OR-matches labels; we post-filter to factory:* and drop
        # anything already factory:queued (the fetch-side guard).
        raw = await provider.fetch_issues(IssueFilters(state="open"))
        out: list[IntakeIssue] = []
        for iss in raw:
            labels = list(getattr(iss, "labels", []) or [])
            lower = {label.lower() for label in labels}
            if "factory:queued" in lower:
                continue
            if not any(label.lower().startswith("factory:") for label in labels):
                continue
            out.append(
                IntakeIssue(
                    number=iss.number,
                    labels=labels,
                    title=getattr(iss, "title", "") or "",
                    body=getattr(iss, "body", "") or "",
                    url=getattr(iss, "url", "") or "",
                )
            )
        return out

    return _run_async(_go())


def _apply_label(cfg: RepoConfig, number: int, label: str) -> None:
    async def _go():
        provider = _provider_for(cfg)
        await provider.apply_labels(number, [label])

    _run_async(_go())


def _comment(cfg: RepoConfig, number: int, body: str) -> None:
    async def _go():
        provider = _provider_for(cfg)
        await provider.add_comment(number, body)

    _run_async(_go())


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> None:
    """POST JSON; raise TerminalIntakeError on 4xx, generic on transport/5xx."""
    import urllib.error
    import urllib.request

    token = os.environ.get("AIFACTORY_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp.read()
    except urllib.error.HTTPError as exc:
        # 4xx is a bad request (won't fix on retry) -> terminal; 5xx -> transient.
        if 400 <= exc.code < 500:
            raise TerminalIntakeError(f"{url} -> HTTP {exc.code}") from exc
        raise RuntimeError(f"{url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} unreachable: {exc}") from exc


def _route_low_medium(cfg: RepoConfig, issue: IntakeIssue, tier: Tier) -> None:
    base = (os.environ.get("AIFACTORY_URL") or "").rstrip("/")
    if not base:
        raise TerminalIntakeError("AIFACTORY_URL unset; cannot route low/medium")
    _post_json(
        f"{base}/api/tasks/from-issue",
        {
            "project_id": cfg.project_id,
            "provider": cfg.provider,
            "repo": cfg.repo,
            "issue_number": issue.number,
            "labels": issue.labels,
            "change_mode": cfg.change_mode,
        },
    )


def _route_hard(cfg: RepoConfig, issue: IntakeIssue, tier: Tier) -> None:
    url = (os.environ.get("PFACTORY_INGEST_URL") or "").rstrip("/")
    if not url:
        raise TerminalIntakeError("PFACTORY_INGEST_URL unset; cannot route hard")
    _post_json(
        url,
        {
            "repo": cfg.repo,
            "provider": cfg.provider,
            "issue_number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
            "autonomy_tier": tier.value,
            "change_mode": cfg.change_mode,
        },
    )


def build_deps() -> PollerDeps:
    """Assemble PollerDeps wired to the real provider + HTTP collaborators."""
    return PollerDeps(
        fetch_issues=_fetch_issues,
        apply_label=_apply_label,
        comment=_comment,
        route_low_medium=_route_low_medium,
        route_hard=_route_hard,
        mark_processed=processed_store.mark_processed,
        unmark_processed=processed_store.unmark_processed,
    )


async def poller_loop(
    *, interval=None, stop=None, deps=None, repos=None, poll_timeout=None
) -> None:
    """Background loop: one poll pass every ``interval`` seconds until stopped.

    Mirrors outbox.relay_loop: blocking provider/HTTP work runs in a worker
    thread; ``stop`` (asyncio.Event) ends the loop at the next tick; a tick error
    never kills the loop.
    """
    import asyncio

    stop = stop or asyncio.Event()
    deps = deps or build_deps()
    repos = repos if repos is not None else load_repo_configs()
    poll_interval = interval if interval is not None else interval_s()
    # #868: a single poll must not be able to wedge the loop forever. poll_once
    # runs blocking provider/HTTP work (``_fetch_issues`` has no hard timeout of
    # its own), so bound each tick — a hung fetch is abandoned (its worker thread
    # is left to finish/die on its own) and the loop keeps ticking rather than
    # going permanently silent. Generous, so a merely-slow poll never trips it.
    # Injectable for tests.
    poll_timeout = (
        poll_timeout if poll_timeout is not None else max(poll_interval * 4, 120.0)
    )
    # #868: emit an idle heartbeat every ~5 min so "started but never polls" is
    # visible — otherwise a healthy poller that only ever sees already-queued
    # issues logs nothing (routed/failed both 0) and is indistinguishable from a
    # dead one, which is exactly how this looked.
    heartbeat_every = max(1, round(300 / poll_interval))
    logger.info(
        "intake poller started (interval=%.0fs, repos=%d)", poll_interval, len(repos)
    )
    tick = 0
    while not stop.is_set():
        tick += 1
        try:
            counts = await asyncio.wait_for(
                asyncio.to_thread(poll_once, repos, deps), timeout=poll_timeout
            )
            if counts.get("routed") or counts.get("failed"):
                logger.info("intake poll: %s", counts)
            elif tick % heartbeat_every == 0:
                logger.info("intake poll heartbeat (idle): %s", counts)
        except asyncio.TimeoutError:
            logger.warning(
                "intake poll exceeded %.0fs and was abandoned this tick — a "
                "provider/network call likely hung; continuing to the next tick",
                poll_timeout,
            )
        except Exception:  # noqa: BLE001 — never let the poller die
            logger.exception("intake poll tick failed (best-effort)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass
    logger.info("intake poller stopped")
