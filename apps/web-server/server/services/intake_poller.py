"""Lifespan wrapper for the RFC-0011 intake poller (#636).

Wires the pure loop logic in ``intake.poller`` to real collaborators (the
GitProvider abstraction for fetch/label/comment, HTTP for routing) and runs it
as a background asyncio task, modeled on ``services/outbox.py::relay_loop`` +
the web-server lifespan.

Env-gated and **off by default**:
  AIFACTORY_INTAKE_POLLER   "true" to enable (default off).
  AIFACTORY_INTAKE_REPOS    JSON list of
      {"provider","repo","project_id"[,"change_mode"][,"base_branch"]}.
  AIFACTORY_INTAKE_INTERVAL_S   poll interval seconds (default 30).
  AIFACTORY_URL                 base URL for the /api/tasks/from-issue route.
  PFACTORY_INGEST_URL           PFactory ingest endpoint for hard-tier routing.
"""

from __future__ import annotations

import contextlib
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
from repo_ref import parse_repo_ref, qualify_repo  # noqa: E402

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


def requeue_after_s() -> float:
    """Grace before a build-less factory:queued issue is re-dispatched (#941).

    Floored at 60s so a build that is merely slow to write its spec is never
    re-opened out from under itself.
    """
    try:
        return max(
            60.0, float(os.environ.get("AIFACTORY_INTAKE_REQUEUE_AFTER_S", "600"))
        )
    except (TypeError, ValueError):
        return 600.0


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
                base_branch=it.get("base_branch"),
            )
        )
    return out


# --------------------------------------------------------------------------
# Real collaborators
# --------------------------------------------------------------------------


def _provider_for(cfg: RepoConfig):
    """The provider for THIS repo config — from the declaration, not the default.

    RFC-0020 3.5. This used to be a bare ``_get_project_provider(cfg.project_id)``,
    which read the AIFactory project's own ``gitProvider`` setting and defaulted
    it to ``github``. A ``RepoConfig`` that plainly said ``"provider": "gitlab"``
    was therefore polled through a GitHub client, and RFC-0011 label intake
    ignored the tenant's declaration entirely.

    ``cfg.repo`` is the provider-qualified reference; where it carries no
    qualification, ``cfg.provider`` (the older per-repo hint in
    ``AIFACTORY_INTAKE_REPOS``) supplies the host, so a deployment configured
    before phase 5 keeps working. The project's settings still supply the
    credential.
    """
    from ..routes.github import _get_project_provider

    # The reference's OWN qualification wins; cfg.provider only fills a gap.
    # Reversing this would let the field's "github" default silently override an
    # explicit gitlab: reference, which is the whole bug.
    return _get_project_provider(cfg.project_id, repo_ref=_declared_ref(cfg))


def _declared_ref(cfg: RepoConfig) -> str:
    """``cfg``'s repo reference, qualified from ``cfg.provider`` if it is not."""
    provider, project = parse_repo_ref(cfg.repo) or ("github", "")
    if provider == "github" and ":" not in (cfg.repo or ""):
        provider = (cfg.provider or "github").strip().lower()
    return qualify_repo(provider, project)


def _run_async(coro):
    """Run a coroutine to completion from the (sync) poller thread."""
    import asyncio

    return asyncio.run(coro)


def _fetch_issues(cfg: RepoConfig) -> list[IntakeIssue]:
    """Fetch open factory:* issues via the provider.

    Includes ``factory:queued`` issues (the guard-2 marker) so the self-heal can
    see an orphaned dispatch (#941); the pure poller still skips a queued issue
    for *routing* unless it is build-less past the grace. ponytail: this stats
    the control plane once per queued issue per tick — cheap at fleet volume; add
    a fetch-side "queued but recent" filter only if a repo ever has thousands.
    """
    from runners.github.providers.protocol import IssueFilters

    async def _go() -> list[IntakeIssue]:
        provider = _provider_for(cfg)
        # The provider OR-matches labels; we post-filter to factory:* (queued
        # issues are kept so the self-heal can re-open a build-less one).
        raw = await provider.fetch_issues(IssueFilters(state="open"))
        out: list[IntakeIssue] = []
        for iss in raw:
            labels = list(getattr(iss, "labels", []) or [])
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


# Colors for the factory:* labels the intake system owns and applies. Used only
# when a label is missing and has to be created on demand (#861).
_FACTORY_LABEL_COLORS = {"factory:queued": "0e8a16", "factory:failed": "b60205"}


def _apply_label(cfg: RepoConfig, number: int, label: str) -> None:
    async def _go():
        provider = _provider_for(cfg)
        try:
            await provider.apply_labels(number, [label])
        except Exception:  # noqa: BLE001
            # #861: gh fails the whole apply if the label doesn't exist in the
            # repo yet, so an intake target that was never seeded with
            # ``factory:queued`` left every routed issue stuck at ``factory:low``
            # (and #870 harder to spot). The intake system OWNS its own
            # ``factory:*`` labels on repos it was explicitly pointed at, so
            # create the missing one (idempotent --force) and retry once. If the
            # create ALSO fails (e.g. no label permission) the caller's ``_safe``
            # wrapper degrades it to a best-effort no-op — routing already
            # succeeded and must not be undone by cosmetic bookkeeping.
            from runners.github.providers.protocol import LabelData  # noqa: PLC0415

            await provider.create_label(
                LabelData(name=label, color=_FACTORY_LABEL_COLORS.get(label, "ededed"))
            )
            await provider.apply_labels(number, [label])

    _run_async(_go())


def _comment(cfg: RepoConfig, number: int, body: str) -> None:
    async def _go():
        provider = _provider_for(cfg)
        await provider.add_comment(number, body)

    _run_async(_go())


def _api_token() -> str | None:
    """Bearer for the AIFactory API that ``_post_json`` calls.

    #847: this used to fall back to ``GH_TOKEN`` — but ``_post_json`` calls our
    OWN API (``/api/tasks/from-issue``), and a GitHub PAT is never a valid
    credential for it, so that branch could only ever produce a 401 (exactly the
    one #844 hit). Fall back to the API's own token instead.
    """
    return os.environ.get("AIFACTORY_TOKEN") or os.environ.get("APP_API_TOKEN")


def _pfactory_token() -> str | None:
    """Bearer for PFactory's API (hard-tier ingest, #874).

    A *sibling's* API, so ``AIFACTORY_TOKEN`` (our own) is not a valid credential
    for it — same reasoning as #847. Mirrors the established sibling convention
    (``tfactory_client.tfactory_config``): a dedicated token, else the shared
    ``APP_API_TOKEN`` every factory pod carries.
    """
    return os.environ.get("PFACTORY_TOKEN") or os.environ.get("APP_API_TOKEN")


def _http_detail(exc: object, limit: int = 300) -> str:
    """The readable reason out of an HTTPError body, as ``": <detail>"`` or "".

    FastAPI reports it as ``{"detail": ...}``; anything else falls back to the
    raw body. Best-effort — a body that cannot be read must never mask the
    status code it is decorating.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — the status code is the load-bearing part
        return ""
    if not raw:
        return ""
    try:
        detail = json.loads(raw).get("detail", raw)
    except (ValueError, AttributeError):
        detail = raw
    text = str(detail).strip().replace("\n", " ")
    return f": {text[:limit]}" if text else ""


def _post_json(
    url: str, payload: dict, timeout: float = 10.0, token: str | None = None
) -> None:
    """POST JSON; raise TerminalIntakeError on 4xx, generic on transport/5xx."""
    import urllib.error
    import urllib.request

    token = token or _api_token()
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
            # Carry the server's reason, not just the status. A terminal error
            # becomes the issue comment, and "HTTP 400" alone tells the human who
            # filed the issue nothing they can act on — the detail (e.g. "no
            # acceptance criteria found") is the whole point of refusing loudly.
            raise TerminalIntakeError(
                f"{url} -> HTTP {exc.code}{_http_detail(exc)}"
            ) from exc
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
            # Integration branch for repos that do not merge via their default
            # branch (the fleet repos integrate via dev): the worktree is cut
            # from it AND the eventual auto-PR targets it.
            "base_branch": cfg.base_branch,
        },
    )


def _route_hard(cfg: RepoConfig, issue: IntakeIssue, tier: Tier) -> None:
    """Route a hard-tier issue into full PFactory planning (RFC-0011, #874).

    PFactory's ``POST /api/plan/sessions/from-issue`` accepts this exact payload
    and turns it into a plan session keyed on ``issue_number`` (the RFC-0001
    correlation key), so the resulting plan -> code -> verify chain threads back
    to the issue a human filed.

    ``PFACTORY_INGEST_URL`` remains the gate (#843). Unset, this still refuses
    LOUDLY with a maintainer-actionable message (which becomes the issue comment
    via the terminal path) rather than silently dropping the issue — an
    unconfigured deployment must degrade honestly, not quietly.
    """
    url = (os.environ.get("PFACTORY_INGEST_URL") or "").rstrip("/")
    if not url:
        raise TerminalIntakeError(
            "this issue is tagged for the hard (full-planning) tier, but "
            "PFACTORY_INGEST_URL is not configured for this deployment, so it "
            "cannot be routed to PFactory automatically — a maintainer should "
            "run PFactory planning for it manually. Low/medium-tier issues are "
            "built automatically."
        )
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
        token=_pfactory_token(),
    )


def _build_exists(cfg: RepoConfig, issue_number: int) -> bool:
    """Whether a build already exists for this issue on the control plane (#941).

    The reliable, conservative signal is the spec dir ``/api/tasks/from-issue``
    writes synchronously (stamped with ``provenance.issue_number``) before it
    dispatches — so its ABSENCE means no build was created. Reuses the exact
    idempotency lookup from-issue itself uses (#878). An unresolvable project is
    treated as "exists" so a lookup failure never triggers a re-dispatch.
    """
    from ..routes.from_issue import _find_existing_spec, _resolve_project_path

    project_path = _resolve_project_path(cfg.project_id)
    if project_path is None:
        return True
    return _find_existing_spec(project_path, issue_number) is not None


def _requeue(cfg: RepoConfig, issue_number: int) -> None:
    """Re-open an orphaned queued issue: drop the guard + release the claim.

    Removing ``factory:queued`` re-exposes the issue to the next poll, and
    deleting the (confirmed) processed row lets that poll claim + route it fresh.
    """

    async def _go():
        provider = _provider_for(cfg)
        await provider.remove_labels(issue_number, ["factory:queued"])

    _run_async(_go())
    processed_store.unmark_processed(cfg.repo, issue_number)


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
        confirm_processed=processed_store.confirm_processed,
        build_exists=_build_exists,
        claimed_at=processed_store.claimed_at,
        requeue=_requeue,
        requeue_after_s=requeue_after_s(),
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
            if counts.get("routed") or counts.get("failed") or counts.get("requeued"):
                logger.info("intake poll: %s", counts)
            elif tick % heartbeat_every == 0:
                logger.info("intake poll heartbeat (idle): %s", counts)
        except TimeoutError:
            logger.warning(
                "intake poll exceeded %.0fs and was abandoned this tick — a "
                "provider/network call likely hung; continuing to the next tick",
                poll_timeout,
            )
        except Exception:  # noqa: BLE001 — never let the poller die
            logger.exception("intake poll tick failed (best-effort)")
        # ponytail: timeout is the normal loop tick, not an error -- it just
        # means the stop event hasn't fired yet
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
    logger.info("intake poller stopped")
