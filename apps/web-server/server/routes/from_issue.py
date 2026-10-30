"""Governed tier-driven ingest from a labelled issue (RFC-0011, #635).

``POST /api/tasks/from-issue`` is the endpoint the label poller (and the GH
Actions ``aifactory-task.yml`` fast path, which referenced a non-existent
``/from-github-issue``) call to turn a labelled issue into a build.

Flow:
  1. Resolve the issue body + labels — either from the request ``payload`` /
     ``labels`` directly, or by fetching via the project's ``GitProvider``
     (provider-agnostic: GitHub / GitLab / Azure DevOps).
  2. Classify the RFC-0011 difficulty tier (``classify_tier`` / ``tier_for``),
     forcing ``hard`` for a ``migration`` rewrite.
  3. Build the tier ``execution`` block (model / skip_planning / review_tier /
     complexity / autonomy_tier) via ``intake.build_execution_block``.
  4. Create a spec (mirroring ``import_github_issues``), apply the execution
     profile to ``task_metadata.json`` (reusing ``apply_execution_profile`` from
     the trusted-plan path), stamp the issue number for RFC-0001 correlation,
     and start the build.

Low/medium ride the skip-planning fast path; hard keeps full planning. Heavy
deps (the agent SDK) are imported lazily so the module stays unit-testable.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server import project_registry
from server.database.engine import get_db
from server.error_ref import client_error
from server.routes.git import run_git_command
from server.routes.project_authz import authorize_project_for_user
from server.services.build_backend import BuildBackendUnavailableError
from server.services.completion import correlation_key, read_issue_repo
from server.services.originator import issue_originator, principal_attribution

from ..tenancy import resolve_tenant, stamp_spec_tenant

# Add the backend dir to sys.path so backend seams resolve (mirrors execution.py).
_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from intake import build_execution_block  # noqa: E402 — needs sys.path above
from pfactory.tiers import classify_parallel, classify_tier, tier_for  # noqa: E402
from repo_ref import parse_repo_ref, qualify_repo  # noqa: E402
from trusted_plan import apply_execution_profile  # noqa: E402

logger = logging.getLogger(__name__)

router = APIRouter()


class FromIssueRequest(BaseModel):
    """Request to ingest a labelled issue as a tier-driven build."""

    project_id: str = Field(..., description="Target AIFactory project id")
    provider: str | None = Field(
        None, description="Git provider hint (github|gitlab|azure_devops)"
    )
    repo: str | None = Field(
        None,
        description=(
            "The task contract's repo reference, optionally provider-qualified "
            "(RFC-0020 3.5): 'owner/repo' | 'gitlab:group/project' | "
            "'azure_devops:org/project/repo'. Decides which git HOST this build "
            "belongs to, outranking the project's gitProvider setting, and is "
            "recorded on the spec so the PR endgame honours it too."
        ),
    )
    issue_number: int | None = Field(
        None, description="Issue/work-item number to fetch via the provider"
    )
    payload: dict | None = Field(
        None, description="Pre-fetched issue dict (title/body/labels); skips fetch"
    )
    labels: list[str] | None = Field(
        None, description="Label names override (e.g. from a webhook)"
    )
    change_mode: str | None = Field(
        None, description="change-mode; 'migration' forces the hard tier"
    )
    auto_continue: bool = Field(True, description="Auto-continue to next phase")
    base_branch: str | None = Field(None, description="Base branch for the worktree")


def _normalize_issue(payload: dict | None, issue_number: int | None = None) -> dict:
    """Pull title/body/number/labels/url out of a provider/gh issue dict.

    ``issue_number`` is the request's own field, used only when the payload
    carries no number of its own. A caller that sends BOTH a pre-fetched payload
    and the number it fetched it by used to lose the number here, and a spec
    written without one correlates on the synthetic ``af-<spec_id>`` key that
    nothing else joins on — which is how a build's whole token spend landed on
    an orphan work item and the cockpit reported $0.00 (#1418).
    """
    payload = payload or {}
    raw_labels = payload.get("labels") or []
    label_names = [
        lbl.get("name", "") if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels
    ]
    number = payload.get("number")
    return {
        "number": issue_number if number is None else number,
        "title": payload.get("title", "") or "",
        "body": payload.get("body", "") or "",
        "url": payload.get("url", "") or payload.get("htmlUrl", "") or "",
        "state": (payload.get("state", "") or "").lower(),
        "labels": [name for name in label_names if name],
    }


def _declared_repo(request: FromIssueRequest) -> str:
    """This request's provider-qualified repo reference (RFC-0020 3.5).

    ``repo``'s own qualification wins; the older ``provider`` hint fills a gap in
    an unqualified one, so a caller written before phase 5 keeps working. The
    other order would let ``provider``'s default silently override an explicit
    ``gitlab:`` reference, which is the bug Factory#366 closes.
    """
    provider, project = parse_repo_ref(request.repo) or ("github", "")
    if not project:
        return ""
    if provider == "github" and ":" not in (request.repo or ""):
        provider = (request.provider or "github").strip().lower()
    return qualify_repo(provider, project)


# Hosts whose provider is unambiguous; self-hosted hosts derive nothing.
_HOST_PROVIDER = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "dev.azure.com": "azure_devops",
}
_PROVIDER_HOST = {v: k for k, v in _HOST_PROVIDER.items()}


def _repo_id(
    value: str | None, base: tuple[str, str] = ("", ""), *, fold: bool = True
) -> tuple[str, str]:
    """``(host, path)`` of a repo path or git URL; host is "" for a bare path.

    Lowercased path without ``.git`` or edge slashes. Azure forms collapse to
    ``dev.azure.com`` + ``org/project/repo``. ``base`` is the project's own
    ``(host, subpath)``: a self-hosted instance under a subpath drops it.
    """
    raw = (value or "").strip()
    host = ""
    if "://" in raw:
        url = urlparse(raw)
        host, raw = url.hostname or "", url.path
    elif "@" in raw.partition(":")[0]:  # scp-like git@host:owner/repo.git
        user_host, _, raw = raw.partition(":")
        host = user_host.rpartition("@")[2]
    host = host.lower()
    path = raw.strip("/").removesuffix(".git")
    path = path.lower() if fold else path  # fold to compare; keep case to store
    if host in {"ssh.dev.azure.com", "vs-ssh.visualstudio.com"}:
        host, path = "dev.azure.com", path.removeprefix("v3/")
    elif host.endswith(".visualstudio.com"):  # org.visualstudio.com/proj/_git/repo
        host, path = "dev.azure.com", f"{host.split('.')[0]}/{path}"
    path = path.replace("/_git/", "/")
    base_host, base_path = base
    if base_path and host == base_host:
        path = path.removeprefix(f"{base_path}/")
    return host, path


def _origin_url(project_path: Path) -> str:
    """The checkout's ``origin`` URL; "" when it cannot be read (local, no network)."""
    result = run_git_command(["remote", "get-url", "origin"], str(project_path))
    return result.get("output", "") if result["success"] else ""


@dataclass(frozen=True)
class _OwnRepo:
    """What a project says its own repository is (#1900)."""

    provider: str | None  # gitProvider, else derived from the own host
    paths: frozenset[str]
    hosts: frozenset[str]
    base: tuple[str, str]  # (host, subpath) of gitBaseUrl


def _project_repos(project_id: str, project_path: Path) -> _OwnRepo:
    """The project's own repo: ``gitRepo``, ``clonedFrom``, then checkout origin."""
    project = project_registry.load_projects().get(project_id) or {}
    settings = project.get("settings") or {}
    base = _repo_id(settings.get("gitBaseUrl"))
    candidates = (
        settings.get("gitRepo"),
        project.get("clonedFrom"),
        _origin_url(project_path),
    )
    own = [_repo_id(c, base) for c in candidates if c]
    hosts = [h for h, _ in own if h] + ([base[0]] if base[0] else [])
    provider = (settings.get("gitProvider") or "").strip().lower() or next(
        (_HOST_PROVIDER[h] for h in hosts if h in _HOST_PROVIDER),
        None if hosts else "github",  # nothing hosted: what the provider defaults to
    )
    if not hosts and provider in _PROVIDER_HOST:
        hosts = [_PROVIDER_HOST[provider]]
    return _OwnRepo(
        provider=provider,
        paths=frozenset(p for _, p in own if p),
        hosts=frozenset(hosts),
        base=base,
    )


def _check_declared_repo(project_id: str, project_path: Path, repo_ref: str) -> str:
    """#1900: a declared repo must be the project's own (its gitToken goes there).

    A declared host must be the project's host; a bare ``owner/repo`` compares
    by path. The declared provider must match the project's; when it cannot be
    derived, a non-GitHub prefix needs ``gitBaseUrl`` so the token never goes
    to a public default host. Returns the normalised ``[provider:]owner/repo``
    to store and pass on ("" when nothing is declared).
    """
    declared = parse_repo_ref(repo_ref)
    if declared is None:
        return ""
    prefix, project = declared
    own = _project_repos(project_id, project_path)
    host, path = _repo_id(project, own.base)
    provider_ok = (
        prefix == own.provider
        if own.provider
        else prefix == "github" or bool(own.base[0])
    )
    if path not in own.paths or (host and host not in own.hosts) or not provider_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"repo {repo_ref!r} is not this project's repository",
        )
    normalised: str = qualify_repo(prefix, _repo_id(project, own.base, fold=False)[1])
    return normalised


async def _fetch_issue_via_provider(
    project_id: str, issue_number: int, *, repo_ref: str | None = None
) -> dict:
    """Fetch an issue through the project's GitProvider (provider-agnostic).

    ``repo_ref`` carries the declared host, so an issue is FETCHED from the same
    place the build will be pushed to. Without it the fetch used the project's
    default provider while the rest of the run used the declaration, which is a
    worse failure than either alone: the issue reads fine and the work lands
    somewhere else.
    """
    from .github import _get_project_provider

    provider = _get_project_provider(project_id, repo_ref=repo_ref)
    issue = await provider.fetch_issue(issue_number)
    kind = getattr(provider, "provider_type", None)
    kind = getattr(kind, "value", kind)
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "url": issue.url,
        "state": issue.state,
        "labels": list(issue.labels),
        # Trusted only here: the provider read it from the host. A caller's
        # payload could name anyone, so the payload path records no originator.
        "originator": issue_originator(
            kind if isinstance(kind, str) else None, issue.author
        ),
    }


def _write_spec(
    project_path: Path,
    spec_id: str,
    issue: dict,
    execution: dict,
    tier_value: str,
    tenant: str = "default",
    repo_ref: str | None = None,
    principal: object = None,
    originator: str | None = None,
) -> Path:
    """Create the spec dir, write requirements.json + apply the execution profile."""
    specs_dir = project_path / ".aifactory" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)

    requirements: dict = {
        "title": issue.get("title") or f"Issue #{issue.get('number')}",
        "description": issue.get("body", ""),
        "source": "rfc0011-intake",
        "created_at": datetime.now(UTC).isoformat(),
        "intake": {"tier": tier_value, "autonomy_tier": tier_value},
        "githubIssue": {
            "number": issue.get("number"),
            "url": issue.get("url", ""),
            "state": issue.get("state", ""),
            "labels": issue.get("labels", []),
            # The provider-qualified reference (RFC-0020 3.5). Recorded HERE
            # because gather_pr_context already reads githubIssue.repo to find
            # the repository, so the host arrives on the endgame's existing path
            # rather than through a new one it would have to be taught. The key
            # name is a legacy of when GitHub was the only host; the value is
            # not GitHub-specific any more.
            "repo": repo_ref or "",
        },
    }
    requirements.update(principal_attribution(principal))
    requirements["originator"] = originator
    # RFC-0001 correlation: stamp the issue number on first write so the cockpit
    # threads plan->code->test instead of minting an orphan card.
    number = issue.get("number")
    provenance: dict = {}
    if isinstance(number, int):
        provenance["issue_number"] = number
    if repo_ref:
        # Mirrors the contract's own provenance.repo, so a spec created from an
        # issue and one created from a signed plan describe their target the
        # same way.
        provenance["repo"] = repo_ref
    if provenance:
        requirements["provenance"] = provenance

    (spec_dir / "requirements.json").write_text(json.dumps(requirements, indent=2))

    # #806: run.py's find_spec only recognizes a spec dir that contains
    # spec.md, and _start_build spawns run.py directly (it never passes the
    # execution.py simple fast path that generates one). Without this file
    # every from-issue tier dies at spawn with "Spec not found".
    spec_md = spec_dir / "spec.md"
    if not spec_md.exists():
        spec_md.write_text(
            f"# {requirements['title']}\n\n{requirements['description']}\n"
        )

    # Reuse the trusted-plan execution-profile writer: maps the snake_case
    # execution block into task_metadata.json (model/skipPlanning/reviewTier/...).
    apply_execution_profile(spec_dir, {"execution": execution})

    # Opt intake builds into the TFactory auto-handoff so a finished autonomous
    # build is INDEPENDENTLY verified — the fleet's whole point. The handoff
    # (maybe_auto_handoff_tfactory) reads auto_handover_tfactory from
    # task_metadata and is itself a no-op unless TFACTORY_BASE_URL is configured,
    # so this is safe when TFactory is absent. Default on; opt out per deployment
    # with AIFACTORY_INTAKE_AUTO_HANDOFF in {0,false,no,off}.
    if _intake_auto_handoff_enabled():
        _set_task_metadata_flag(spec_dir, "auto_handover_tfactory", True)
    # Multi-tenancy (#925): record the creating tenant (no-op unless enabled).
    stamp_spec_tenant(spec_dir, tenant)
    return spec_dir


def _find_existing_spec(
    project_path: Path, issue_number: int, repo_ref: str | None = None
) -> str | None:
    """Return the spec id already recorded for this issue number, if any (#878).

    Keys on ``requirements.provenance.issue_number``, which ``_write_spec``
    stamps on first write — so a redelivered issue adopts the existing spec
    instead of minting a duplicate build. When both the stored and the requested
    repo are known and differ, it is another repo's issue and does not match (#1775).
    """
    specs_dir = project_path / ".aifactory" / "specs"
    if not specs_dir.is_dir():
        return None
    for spec_dir in sorted(specs_dir.iterdir()):
        req_file = spec_dir / "requirements.json"
        if not req_file.is_file():
            continue
        try:
            requirements = json.loads(req_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        provenance = requirements.get("provenance")
        if (
            isinstance(provenance, dict)
            and provenance.get("issue_number") == issue_number
        ):
            stored = provenance.get("repo")
            if repo_ref and stored and stored != repo_ref:
                continue
            return spec_dir.name
    return None


def _intake_auto_handoff_enabled() -> bool:
    """Whether intake builds auto-hand off to TFactory (default on)."""
    return (
        os.environ.get("AIFACTORY_INTAKE_AUTO_HANDOFF") or ""
    ).strip().lower() not in {"0", "false", "no", "off"}


def _set_task_metadata_flag(spec_dir: Path, key: str, value: object) -> None:
    """Merge a single flag into task_metadata.json (best-effort)."""
    tm_file = spec_dir / "task_metadata.json"
    try:
        tm = json.loads(tm_file.read_text()) if tm_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        tm = {}
    if isinstance(tm, dict):
        tm[key] = value
        with contextlib.suppress(OSError):
            tm_file.write_text(json.dumps(tm, indent=2))


@router.post("/from-issue")
async def create_from_issue(
    request: FromIssueRequest,
    raw_request: Request = None,  # FastAPI injects; None lets direct callers omit
    db: Annotated[AsyncSession | None, Depends(get_db)] = None,
):
    """Ingest a labelled issue and start a tier-driven governed build."""
    from .tasks import get_next_spec_id

    # #1898: project_id is in the body, so require_project_access (path/query)
    # cannot see it; apply its shared rule here. Service token and auth-disabled
    # pass; raw_request is None only for in-process callers (#1466).
    if raw_request is not None:
        if db is None:  # FastAPI always injects it; never skip authz silently
            raise HTTPException(status_code=500, detail="Authorization unavailable")
        await authorize_project_for_user(
            getattr(raw_request.state, "user", None), request.project_id, db, "member"
        )

    projects_path = _resolve_project_path(request.project_id)
    if projects_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {request.project_id} not found",
        )
    repo_ref = _check_declared_repo(
        request.project_id, projects_path, _declared_repo(request)
    )

    # 1. Resolve the issue: explicit payload wins; otherwise fetch via provider.
    originator: str | None = None
    if request.payload is not None:
        issue = _normalize_issue(request.payload, request.issue_number)
    elif request.issue_number is not None:
        try:
            fetched = await _fetch_issue_via_provider(
                request.project_id,
                request.issue_number,
                repo_ref=repo_ref,
            )
        except Exception as exc:  # noqa: BLE001 — surface a clean 502
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=client_error(
                    logger, f"Failed to fetch issue #{request.issue_number}", exc
                ),
            ) from exc
        issue = _normalize_issue(fetched)
        originator = fetched.get("originator")
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide either payload or issue_number",
        )

    # Idempotency by issue number (#878): a redelivered issue (e.g. reclaim
    # after a crash between task creation and claim confirmation) must not
    # mint a duplicate spec/build — no-op onto the existing one.
    issue_number = issue.get("number")
    if isinstance(issue_number, int):
        existing_spec_id = _find_existing_spec(
            projects_path, issue_number, repo_ref or None
        )
        if existing_spec_id is not None:
            return {
                "success": True,
                "task_id": f"{request.project_id}:{existing_spec_id}",
                "spec_id": existing_spec_id,
                "deduplicated": True,
                "issue_number": issue_number,
                # The stored spec's repo is the truth for a dedup (#1775).
                "correlation_key": correlation_key(
                    existing_spec_id,
                    issue_number,
                    read_issue_repo(
                        projects_path / ".aifactory" / "specs" / existing_spec_id
                    ),
                ),
                "message": (
                    f"Issue #{issue_number} already ingested as spec "
                    f"{existing_spec_id}; not creating a duplicate."
                ),
            }

    # Label override (e.g. from a webhook) takes precedence over the issue's own.
    if request.labels is not None:
        issue["labels"] = list(request.labels)

    # 2. Classify the tier (highest-wins), migration forces hard.
    labelled_tier = classify_tier(issue["labels"])

    class _Carrier:
        tier = labelled_tier

    tier = tier_for(_Carrier(), change_mode=request.change_mode)

    # 2b. Parallelism is a separate axis: factory:parallel / factory:serial
    # (opt-out wins), factory:workers=N tunes the cap. Unlabelled => the
    # AIFACTORY_INTAKE_PARALLEL deployment default (off).
    parallel, workers = classify_parallel(issue["labels"])

    # 3. Build the tier execution block. parallel/workers land in
    # task_metadata.json via apply_execution_profile, where the agent service
    # reads them back when it starts the build.
    execution = build_execution_block(
        tier,
        change_mode=request.change_mode,
        parallel=parallel,
        workers=workers,
    )

    # 4. Create the spec + apply the profile + start the build.
    project_path = projects_path
    spec_id = get_next_spec_id(project_path, issue["title"] or "intake-task")
    spec_dir = _write_spec(
        project_path,
        spec_id,
        issue,
        execution,
        tier.value,
        tenant=resolve_tenant(raw_request),
        repo_ref=repo_ref,
        principal=getattr(raw_request.state, "user", None) if raw_request else None,
        originator=originator,
    )
    # Persist the integration branch so the PR endgame targets it too —
    # gather_pr_context reads task_metadata.base_branch; without this a fleet
    # repo that integrates via dev would get its auto-PR opened against main.
    if request.base_branch:
        _set_task_metadata_flag(spec_dir, "base_branch", request.base_branch)

    task_id = f"{request.project_id}:{spec_id}"
    await _start_build(
        task_id=task_id,
        project_path=project_path,
        spec_id=spec_id,
        execution=execution,
        auto_continue=request.auto_continue,
        base_branch=request.base_branch,
    )

    return {
        "success": True,
        "task_id": task_id,
        "spec_id": spec_id,
        "tier": tier.value,
        "execution": execution,
        "issue_number": issue.get("number"),
        "correlation_key": correlation_key(
            spec_id, issue.get("number"), repo_ref or None
        ),
        "message": f"Ingested issue as {tier.value}-tier build ({execution['model']}).",
    }


def _resolve_project_path(project_id: str) -> Path | None:
    """Resolve a project id to its filesystem path (lazy import of projects)."""
    from server.project_registry import load_projects

    projects = load_projects()
    if project_id not in projects:
        return None
    return Path(projects[project_id]["path"])


async def _start_build(
    *,
    task_id: str,
    project_path: Path,
    spec_id: str,
    execution: dict,
    auto_continue: bool,
    base_branch: str | None,
) -> None:
    """Start the build via the agent service (lazy import; heavy SDK deps)."""
    from ..services.agent_service import get_agent_service
    from ..websockets.events import emit_task_status

    agent_service = get_agent_service()
    # skip_planning fast path => force past the plan-review gate; hard keeps it.
    skip_planning = bool(execution.get("skip_planning"))
    try:
        await agent_service.start_task_execution(
            task_id=task_id,
            project_path=project_path,
            spec_id=spec_id,
            auto_continue=auto_continue,
            base_branch=base_branch,
            mode="full",
            force=skip_planning,
        )
        await emit_task_status(task_id, "in_progress")
    except BuildBackendUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=client_error(logger, "Failed to start build from issue", exc),
        ) from exc
