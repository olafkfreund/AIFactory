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

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..tenancy import resolve_tenant, stamp_spec_tenant

# Add the backend dir to sys.path so backend seams resolve (mirrors execution.py).
_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from intake import build_execution_block  # noqa: E402 — needs sys.path above
from pfactory.tiers import classify_parallel, classify_tier, tier_for  # noqa: E402
from repo_ref import parse_repo_ref, qualify_repo  # noqa: E402
from trusted_plan import apply_execution_profile  # noqa: E402

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


def _normalize_issue(payload: dict | None) -> dict:
    """Pull title/body/number/labels/url out of a provider/gh issue dict."""
    payload = payload or {}
    raw_labels = payload.get("labels") or []
    label_names = [
        lbl.get("name", "") if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels
    ]
    return {
        "number": payload.get("number"),
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
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "url": issue.url,
        "state": issue.state,
        "labels": list(issue.labels),
    }


def _write_spec(
    project_path: Path,
    spec_id: str,
    issue: dict,
    execution: dict,
    tier_value: str,
    tenant: str = "default",
    repo_ref: str | None = None,
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
        "created_at": datetime.now().isoformat(),
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


def _find_existing_spec(project_path: Path, issue_number: int) -> str | None:
    """Return the spec id already recorded for this issue number, if any (#878).

    Keys on ``requirements.provenance.issue_number``, which ``_write_spec``
    stamps on first write — so a redelivered issue adopts the existing spec
    instead of minting a duplicate build.
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
        try:
            tm_file.write_text(json.dumps(tm, indent=2))
        except OSError:
            pass


@router.post("/from-issue")
async def create_from_issue(
    request: FromIssueRequest,
    raw_request: Request = None,  # noqa: RUF013 — FastAPI injects; None lets direct callers omit
):
    """Ingest a labelled issue and start a tier-driven governed build."""
    from .tasks import get_next_spec_id

    projects_path = _resolve_project_path(request.project_id)
    if projects_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {request.project_id} not found",
        )

    # 1. Resolve the issue: explicit payload wins; otherwise fetch via provider.
    if request.payload is not None:
        issue = _normalize_issue(request.payload)
    elif request.issue_number is not None:
        try:
            fetched = await _fetch_issue_via_provider(
                request.project_id,
                request.issue_number,
                repo_ref=_declared_repo(request),
            )
        except Exception as exc:  # noqa: BLE001 — surface a clean 502
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch issue #{request.issue_number}: {exc}",
            ) from exc
        issue = _normalize_issue(fetched)
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
        existing_spec_id = _find_existing_spec(projects_path, issue_number)
        if existing_spec_id is not None:
            return {
                "success": True,
                "task_id": f"{request.project_id}:{existing_spec_id}",
                "spec_id": existing_spec_id,
                "deduplicated": True,
                "issue_number": issue_number,
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
        repo_ref=_declared_repo(request),
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
        "message": f"Ingested issue as {tier.value}-tier build ({execution['model']}).",
    }


def _resolve_project_path(project_id: str) -> Path | None:
    """Resolve a project id to its filesystem path (lazy import of projects)."""
    from ..project_store import load_projects

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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start build from issue: {exc}",
        ) from exc
