"""Outbound TFactory transport (epic #327, #337).

When a governed PFactory child routes to TFactory (``handoff:tfactory`` /
``type:testing``), AIFactory POSTs the spec + its ``pfactory:meta`` to
TFactory's HTTP API for test generation. Symmetric with the inbound correction
receiver (``qa/correction.py``, #317).

Configuration (environment):

* ``TFACTORY_BASE_URL``    — e.g. ``https://tfactory.internal`` (required to send)
* ``TFACTORY_TOKEN``       — bearer token (optional)
* ``TFACTORY_HANDOFF_PATH``— endpoint path, default ``/api/handoff``

Graceful by design: when ``TFACTORY_BASE_URL`` is unset, :func:`send_handoff`
returns ``{"sent": False, "reason": "not_configured"}`` and never raises — the
caller still records the local handoff marker. The HTTP poster is injectable so
tests need no network.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

__all__ = [
    "build_handoff_payload",
    "load_task_contract",
    "load_tfactory_block",
    "maybe_auto_handoff_tfactory",
    "send_handoff",
    "tfactory_config",
    "wants_auto_handoff",
]

# (url, json_payload, headers) -> {"status": int, "ok": bool, "body": str}
Poster = Callable[[str, dict, dict], Awaitable[dict]]

# TFactory's self-contained intake: create a test task from a raw spec, no
# shared workspace/branch required (POST {project_id, spec_id, spec_text}).
_DEFAULT_PATH = "/api/specs/ingest"


def tfactory_config(env: dict | None = None) -> dict:
    """Read TFactory transport config from the environment."""
    env = env if env is not None else os.environ
    return {
        "base_url": (env.get("TFACTORY_BASE_URL") or "").rstrip("/"),
        # TFactory's auth middleware requires a bearer token on /api/*. Prefer a
        # dedicated TFACTORY_TOKEN; fall back to the shared APP_API_TOKEN that
        # every factory pod carries (factory-secrets) — without it the handoff
        # 401s (#517).
        "token": env.get("TFACTORY_TOKEN") or env.get("APP_API_TOKEN") or "",
        "path": env.get("TFACTORY_HANDOFF_PATH") or _DEFAULT_PATH,
    }


def load_tfactory_block(spec_dir: Path) -> dict:
    """Read the Task Contract v2 ``tfactory`` block from implementation_plan.json.

    PFactory computes this block (lanes/frameworks/endpoints/coverage/mutation/
    security/ac_to_code_map) and it is installed verbatim by trusted_plan ingest.
    Returns ``{}`` when absent (v1 plans) or unreadable — TFactory then falls
    back to its own inference.
    """
    plan_file = Path(spec_dir) / "implementation_plan.json"
    if not plan_file.exists():
        return {}
    try:
        plan = json.loads(plan_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    block = plan.get("tfactory")
    return block if isinstance(block, dict) else {}


def load_task_contract(spec_dir: Path) -> dict:
    """Return the full signed Task Contract when it carries RFC-0002 markers,
    else ``{}``.

    Reads ``context/task_contract.json`` FIRST: the trusted-plan ingest stashes
    the signed contract there (a build-safe location), because the executor
    rewrites ``implementation_plan.json`` into AIFactory's runtime format during
    the build — dropping the contract's ``tfactory`` block (lanes / frameworks /
    ``ac_to_code_map``) plus ``contract_version``/``approval``. Falls back to
    ``implementation_plan.json`` for plans installed before this stash existed.
    Sending the WHOLE contract on the handoff lets TFactory persist it to its own
    ``context/task_contract.json`` and test the DECLARED acceptance criteria
    instead of inferring (#71 Phase 3). For AIFactory's own (create-and-run)
    plans — no contract markers anywhere — this returns ``{}`` so TFactory infers.
    """
    spec_dir = Path(spec_dir)
    for candidate in (
        spec_dir / "context" / "task_contract.json",
        spec_dir / "implementation_plan.json",
    ):
        if not candidate.exists():
            continue
        try:
            plan = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(plan, dict) and (
            "tfactory" in plan or "contract_version" in plan or "approval" in plan
        ):
            return plan
    return {}


def _verify_phase_models(spec_dir: Path) -> dict[str, str]:
    """Verify-lane models for TFactory, derived from the build's ``phaseModels``.

    Reads ``task_metadata.json``'s ``phaseModels``; verification is judgment work
    (gen-functional / evaluator / planner / qa), so every TFactory phase uses the
    build's ``qa`` model (falling back to ``planning``/``spec``) — keeping verify on
    the SAME provider as the build rather than TFactory's default sonnet. Returns
    ``{}`` when the task set no ``phaseModels`` (so default behaviour is unchanged).
    """
    try:
        meta = json.loads((Path(spec_dir) / "task_metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    pm = meta.get("phaseModels") if isinstance(meta, dict) else None
    if not isinstance(pm, dict) or not pm:
        return {}
    verify = pm.get("qa") or pm.get("planning") or pm.get("spec")
    if not isinstance(verify, str) or not verify:
        return {}
    return dict.fromkeys(
        ("spec", "planning", "coding", "qa", "qa_fixer", "test_gen"), verify
    )


def build_handoff_payload(
    spec_id: str,
    requirements: dict | None,
    classification: Any,
    metadata: dict | None,
    tfactory: dict | None = None,
    spec_dir: Path | None = None,
) -> dict:
    """Build the JSON payload AIFactory sends to TFactory for a handoff.

    When the spec carries a Task Contract v2 ``tfactory`` block (RFC-0002), it is
    included so TFactory plans tests from declared lanes/frameworks/endpoints/
    scope instead of inferring them. Omitted (empty) for v1 specs.

    When ``spec_dir`` is given and a mutation ledger was recorded this run (#476,
    ``AIFACTORY_MUTATION_LEDGER``), the ledger rides along as handover evidence so
    TFactory sees exactly what the coder changed. Additive/best-effort.
    """
    requirements = requirements or {}
    gh = requirements.get("githubIssue") if isinstance(requirements, dict) else None
    labels = gh.get("labels", []) if isinstance(gh, dict) else []
    payload = {
        "source": "aifactory",
        "taxonomy": "v1",
        "spec_id": spec_id,
        "title": requirements.get("title"),
        "description": requirements.get("description"),
        "labels": labels,
        "handoff": getattr(classification, "handoff", None),
        "types": list(getattr(classification, "types", ()) or ()),
        "priority": getattr(classification, "priority", None),
        "pfactory_meta": metadata or {},
        "tfactory": tfactory or {},
    }
    if spec_dir is not None:
        try:
            from agents.mutation_ledger import MutationLedger

            mutations = MutationLedger(spec_dir).read()
            if mutations:
                payload["mutations"] = mutations
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass
    return payload


# TFactory's spec parser accepts an "## Acceptance Criteria" heading or "AC#N:".
_ACCEPTANCE_RE = re.compile(
    r"(^\s*#+\s*acceptance\s+criteria)|(\bAC\s*#?\s*\d+\s*:)",
    re.IGNORECASE | re.MULTILINE,
)
_SUCCESS_RE = re.compile(
    r"^\s*#+\s*success\s+criteria\s*$(.*?)(?=^\s*#+\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _collect_acceptance_criteria(req: dict, spec_text: str) -> list[str]:
    """Best-effort acceptance-criteria bullets for TFactory's spec ingest (#517).

    Prefers requirements.json's ``acceptance_criteria``; falls back to a
    ``## Success Criteria`` section in the spec, then user requirements, then a
    synthesized line from the title — so the ingest never 400s on a missing
    criteria section.
    """
    ac = req.get("acceptance_criteria") or req.get("acceptanceCriteria")
    if isinstance(ac, list) and ac:
        return [str(c).strip().lstrip("-* ").strip() for c in ac if str(c).strip()]
    m = _SUCCESS_RE.search(spec_text or "")
    if m:
        bullets = [
            ln.strip().lstrip("-* ").strip()
            for ln in m.group(1).splitlines()
            if ln.strip().startswith(("-", "*"))
        ]
        if bullets:
            return bullets
    ur = req.get("user_requirements") or req.get("userRequirements")
    if isinstance(ur, list) and ur:
        return [str(c).strip() for c in ur if str(c).strip()]
    title = req.get("title")
    return [f"The feature works as described: {title}"] if title else []


def _aifactory_project_name(spec_dir: Path) -> str:
    """Derive the project name from the workspace layout
    (.../workspaces/<project>/.aifactory/specs/<spec_id>)."""
    parts = Path(spec_dir).resolve().parts
    if "workspaces" in parts:
        i = parts.index("workspaces")
        if i + 1 < len(parts):
            return parts[i + 1]
    try:
        return Path(spec_dir).parents[2].name
    except IndexError:
        return ""


def _project_dir(spec_dir: Path) -> Path:
    """The project root for a spec: spec_dir == <project>/.aifactory/specs/<spec_id>."""
    p = Path(spec_dir).resolve()
    return p.parents[2] if len(p.parents) >= 3 else p.parent


def _build_worktree(spec_dir: Path, spec_id: str) -> Path:
    """The build worktree for a spec: <project>/.aifactory/worktrees/tasks/<spec_id>."""
    return _project_dir(spec_dir) / ".aifactory" / "worktrees" / "tasks" / spec_id


# Branches that are never a valid build OUTPUT — a build always lands on
# ``aifactory/<spec_id>``. If the control-plane worktree reports one of these as
# HEAD (the kubejob path builds inside the k8s Job and leaves the control-plane
# worktree on the base branch), it must NOT become the verify source_branch, or
# TFactory checks out base and cannot find the built code (#938).
_BASE_BRANCHES = frozenset({"main", "master", "HEAD", ""})


def _task_base_branch(spec_dir: Path) -> str:
    """This task's integration branch, per ``task_metadata.base_branch``.

    Needed because ``_BASE_BRANCHES`` above can only name the conventional
    ones. A repo that integrates via ``dev`` (every Factory repo does, and
    AIFACTORY_INTAKE_REPOS configures TFactory that way) left the hard-coded
    set unmatched, so the control-plane worktree's HEAD -- sitting on ``dev``
    after a kubejob build -- was sent to TFactory as the verify source_branch.
    TFactory then checked out ``dev``, which does not contain the built code,
    generated tests against the unbuilt feature, watched all of them fail, and
    correctly refused to commit permanently-red tests. The run looked like a
    verification failure; it was a hollow verify (#980, TFactory #729).
    """
    try:
        meta = json.loads((Path(spec_dir) / "task_metadata.json").read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("base_branch") or meta.get("baseBranch") or "")


def _build_branch(spec_id: str) -> str:
    """The branch a build creates/pushes for a spec.

    Fixed convention, matching ``core.worktree.WorktreeManager.get_branch_name``:
    ``aifactory/<spec_id>``. Reliable even when the local worktree is gone — the
    build already pushed this branch to origin.
    """
    return f"aifactory/{spec_id}"


def _git_stdout(cwd: Path, args: list[str]) -> str:
    """Run ``git <args>`` in ``cwd`` and return trimmed stdout (empty on failure)."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60
    ).stdout.strip()


def _project_git_url(spec_dir: Path) -> str | None:
    """The origin URL of the project's shared base repo (``<project>/.git``).

    Resolves the repo even when the build worktree is absent or not a valid git
    checkout (RFC-0017 packed build: outputs repacked via MinIO, so the
    control-plane worktree's gitdir pointer is broken but the base repo's
    ``.git/config`` still holds origin). Best-effort: returns ``None`` when
    unresolvable — never raises.
    """
    try:
        return (
            _git_stdout(_project_dir(spec_dir), ["remote", "get-url", "origin"]) or None
        )
    except Exception:  # noqa: BLE001 — handoff prep must never break task completion
        return None


def _authed_push_url(url: str) -> str:
    """``url`` with a token injected for authenticated push, when one is available.

    Unchanged https URL when there is no token or it is not a github.com https
    remote (ssh remotes carry their own auth).
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


def _remote_tip(repo: Path, push_url: str, branch: str) -> str:
    """The commit ``origin`` currently has for ``branch`` (``ls-remote``), or ""."""
    line = _git_stdout(repo, ["ls-remote", push_url, branch])
    return line.split()[0] if line else ""


def _build_branch_commit(repo: Path, build_branch: str) -> str:
    """The commit ``build_branch`` points at in ``repo``, or "" if it is absent.

    ``--verify --quiet`` so a missing ref returns "" instead of noise on stderr.
    """
    return _git_stdout(
        repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{build_branch}"]
    )


def _locate_build_commit(spec_dir: Path, spec_id: str) -> tuple[Path, str] | None:
    """Find the repo that HOLDS the built commit and that commit's sha, or None.

    The built code lives on the ``aifactory/<spec>`` branch, but WHERE that ref
    resolves depends on the build path. On the RFC-0017 packed path the build runs
    inside the k8s Job and the control-plane build clone is left on the BASE branch
    (``main``); the built branch ref is unpacked into the PROJECT repo, not the
    build clone. So pushing the build clone's HEAD pushes base, not the build
    (#1007). Resolve the build branch in the project repo first, then the build
    clone (the non-packed path, where the clone genuinely sits on the build
    branch). Return the repo + sha of whichever holds it.
    """
    build_branch = _build_branch(spec_id)
    for cand in (_project_dir(spec_dir), _build_worktree(spec_dir, spec_id)):
        if cand.is_dir():
            sha = _build_branch_commit(cand, build_branch)
            if sha:
                return cand, sha
    return None


def _git_info_and_push(spec_dir: Path, spec_id: str) -> tuple[str | None, str | None]:
    """Return ``(git_url, source_branch)`` for the build, pushing the BUILD BRANCH
    to origin so TFactory (separate PVC) can fetch the built code.

    Pushes ``aifactory/<spec>`` resolved from whichever local repo holds it (the
    project repo on the RFC-0017 packed path, the build clone otherwise) --
    explicitly the built commit onto ``refs/heads/aifactory/<spec>``, never the
    build clone's HEAD, which on the packed path is the BASE branch. Pushing base
    left the build off origin and handed TFactory a tree WITHOUT the build:
    pre-flight then rejects the missing symbols and the run reads as an ordinary
    failure (#1007, the "verify the wrong tree" class, cf. TFactory #729).

    The push is ``--force``: the per-spec ``aifactory/<spec>`` branch is
    build-owned, the local commit is its truth, and it cannot clobber anyone
    else's work -- this also repairs a diverged remote tip (only-base-pushed). We
    then VERIFY origin carries the built commit; if it still does not, return the
    branch as unusable (``(url, None)``) and log, rather than claim a stale tree
    is verifiable.

    Best-effort and never raises: returns ``(None, None)`` when nothing local holds
    the build branch (the caller then falls back to the branch convention).
    """
    located = _locate_build_commit(spec_dir, spec_id)
    if located is None:
        return None, None
    repo, sha = located
    build_branch = _build_branch(spec_id)
    try:
        url = _git_stdout(repo, ["remote", "get-url", "origin"])
        if not url:
            return None, None
        push_url = _authed_push_url(url)
        subprocess.run(
            ["git", "push", "--force", push_url, f"{sha}:refs/heads/{build_branch}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Verify origin actually carries the built commit now — a swallowed push
        # failure sending TFactory to a stale tree was the whole #1007 bug.
        if _remote_tip(repo, push_url, build_branch) != sha:
            _log.warning(
                "[handoff] build commit %s did not land on origin/%s after push; "
                "refusing the branch so TFactory does not verify a stale tree (#1007)",
                sha[:12],
                build_branch,
            )
            return url, None
        return url, build_branch
    except Exception:  # noqa: BLE001 - handoff prep must never break the build
        return None, None


def _recorded_commit_count(spec_dir: Path) -> int | None:
    """Commits the build itself recorded in ``memory/build_commits.json``, or None.

    ``RecoveryManager`` creates this ledger at the start of every build and
    appends to it whenever a coding session leaves a new commit behind (both a
    completed subtask and one that only made partial progress), so an EXISTING
    ledger with an empty ``commits`` list is the build's own record that it
    committed nothing. A missing or unparseable ledger says nothing at all and
    returns ``None``.

    This is the only evidence source that survives the kubejob path: the build
    runs inside the k8s Job, and its memory tree is fetched back into the
    control-plane spec dir on completion (#1038) while the local worktree stays
    on the base branch. Without it every kubejob build was unmeasurable, which
    is how #1070 handed a branch identical to main to TFactory.
    """
    try:
        data = json.loads(
            (Path(spec_dir) / "memory" / "build_commits.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    commits = data.get("commits") if isinstance(data, dict) else None
    return len(commits) if isinstance(commits, list) else None


def build_commit_count(spec_dir: Path, spec_id: str) -> int | None:
    """Commits the build added on top of its base branch, or ``None`` if unknowable.

    ``None`` and ``0`` mean different things and callers must not conflate them:
    ``0`` is the measured fact that the build wrote nothing, while ``None`` says
    the question could not be answered here.

    Unknowable covers two cases, and the second one is the whole reason this
    function has a HEAD check. There is no worktree at all — the RFC-0017 packed
    path returns outputs via MinIO and leaves the control-plane worktree's gitdir
    pointer broken. OR the worktree exists but is not the build: the kubejob path
    builds inside the k8s Job and leaves the control-plane worktree sitting on
    the BASE branch, exactly as the ``_BASE_BRANCHES`` comment above says.

    Counting commits against a worktree still on ``dev`` yields 0 for EVERY
    kubejob build, empty or not. That shipped, and it blocked a real build — 7
    commits, 5 files, 1247 added lines — from ever reaching verify. It looked
    correct only because the run it was written for happened to be genuinely
    empty as well. So the count is trusted only when HEAD is the branch the build
    is supposed to have produced.

    When git cannot answer, the build's own commit ledger does (#1070) — that is
    the ONLY reason a kubejob build is measurable at all. Git wins where both
    speak: a worktree parked on the build branch is the ground truth about what
    the branch holds, where the ledger can only be stale.
    """
    git = _git_commit_count(spec_dir, spec_id)
    return git if git is not None else _recorded_commit_count(spec_dir)


def _git_commit_count(spec_dir: Path, spec_id: str) -> int | None:
    """``build_commit_count``'s git half — see its docstring for the HEAD check."""
    wt = _build_worktree(spec_dir, spec_id)
    if not wt.is_dir():
        return None
    if _git_stdout(wt, ["rev-parse", "--abbrev-ref", "HEAD"]) != _build_branch(spec_id):
        return None
    base = _task_base_branch(spec_dir) or "main"
    for ref in (f"origin/{base}", base):
        out = _git_stdout(wt, ["rev-list", "--count", f"{ref}..HEAD"])
        if out.isdigit():
            return int(out)
    return None


def _issue_from_requirements(req: dict[str, Any]) -> int | None:
    """The origin GitHub issue number from requirements.json, or None. #964

    Reads `githubIssue.number` first, then `provenance.issue_number`.
    """
    gh = req.get("githubIssue")
    if isinstance(gh, dict) and isinstance(gh.get("number"), int):
        return int(gh["number"])
    prov = req.get("provenance")
    if isinstance(prov, dict) and isinstance(prov.get("issue_number"), int):
        return int(prov["issue_number"])
    return None


def build_ingest_payload(spec_dir: Path, spec_id: str) -> dict:
    """Build the payload for TFactory's self-contained spec intake
    (``POST /api/specs/ingest``): ``{project_id, spec_id, spec_text}``.

    ``project_id`` is the TFactory project to ingest into — resolved by name
    from the AIFactory workspace (TFactory carries a matching project),
    overridable via ``TFACTORY_PROJECT_ID``. ``spec_text`` is the finished
    spec.md (falls back to the requirements description). #517.
    """
    spec_dir = Path(spec_dir)
    project_id = os.environ.get("TFACTORY_PROJECT_ID") or _aifactory_project_name(
        spec_dir
    )
    spec_text = ""
    spec_md = spec_dir / "spec.md"
    if spec_md.exists():
        spec_text = spec_md.read_text()
    req: dict = {}
    try:
        req = json.loads((spec_dir / "requirements.json").read_text())
    except (OSError, ValueError):
        req = {}
    if not spec_text.strip():
        spec_text = req.get("description") or req.get("title") or ""
    # TFactory's spec parser requires an "## Acceptance Criteria" section (or
    # "AC#N:" lines). AIFactory's quick specs use "## Success Criteria" or carry
    # criteria in requirements.json, so without this the ingest 400s
    # ("no acceptance criteria found"). Normalize: if the spec_text lacks the
    # heading, append one built from the best criteria source we have (#517).
    if not _ACCEPTANCE_RE.search(spec_text or ""):
        criteria = _collect_acceptance_criteria(req, spec_text)
        if criteria:
            spec_text = (
                (spec_text or "").rstrip()
                + "\n\n## Acceptance Criteria\n"
                + ("\n".join(f"- {c}" for c in criteria))
            )
    payload = {
        "project_id": project_id,
        "spec_id": spec_id,
        "spec_text": spec_text,
        "format": "markdown",
    }
    # Carry the full signed Task Contract so TFactory tests the DECLARED ACs
    # (tfactory block: lanes/frameworks/ac_to_code_map) rather than inferring
    # from spec_text. Present only for trusted plans; absent → TFactory infers.
    contract = load_task_contract(spec_dir)
    # Propagate the build's per-phase model choice to TFactory's verify lanes so a
    # non-default (e.g. Ollama) build is VERIFIED on the same provider instead of
    # silently falling back to TFactory's default (sonnet). The choice lives in
    # task_metadata.json's phaseModels; carry it on the contract's
    # execution.phase_models, which TFactory's ingest turns into its own
    # task_metadata.json (get_phase_model reads that). Additive: a real signed
    # contract's execution block is preserved; we only fill phase_models we add.
    verify_pm = _verify_phase_models(spec_dir)
    if verify_pm:
        contract = dict(contract or {})
        execution = dict(contract.get("execution") or {})
        merged = {**verify_pm, **(execution.get("phase_models") or {})}
        execution["phase_models"] = merged
        contract["execution"] = execution
    # Thread the origin GitHub issue so TFactory can correlate the verify task
    # with its build + plan (it reads contract.provenance.github_issue). The
    # label-driven fast path carries no PFactory plan, so backfill from
    # requirements.json (githubIssue.number / provenance.issue_number). #964
    _issue_no = _issue_from_requirements(req)
    if _issue_no is not None:
        contract = dict(contract or {})
        _prov = dict(contract.get("provenance") or {})
        _prov.setdefault("github_issue", _issue_no)
        contract["provenance"] = _prov
    if contract:
        payload["contract"] = contract
    # PARR seam: hand TFactory the repo + the build branch so it can fetch the
    # ACTUAL built code (separate PVC). Pushes the branch (best-effort) and lets
    # TFactory self-register the project from git_url when it isn't pre-registered.
    git_url, source_branch = _git_info_and_push(spec_dir, spec_id)
    # Fall back to the known branch convention + project repo when the local build
    # worktree is absent or not a valid git checkout (RFC-0017 packed build repacks
    # outputs via MinIO, leaving no git-valid worktree on the control plane). The
    # branch was already pushed to origin during the build, so TFactory can still
    # fetch it; without source_branch it verifies BASE and cannot find the built
    # code (#893). A detected BASE branch (main/master) is likewise unusable — the
    # kubejob path builds inside the Job and leaves this worktree on base, so
    # trusting its HEAD sends TFactory to verify base and every intake build fails
    # at test with the built module "not found" (#938).
    # Any base branch is unusable as a verify source, not just main/master --
    # see _task_base_branch for what that cost on a dev-based repo. Inlined
    # rather than bound to a local: this function is at its statement ceiling.
    if (
        not source_branch
        or source_branch in _BASE_BRANCHES
        or source_branch == _task_base_branch(spec_dir)
    ):
        source_branch = _build_branch(spec_id)
    if not git_url:
        git_url = _project_git_url(spec_dir)
    if git_url:
        payload["git_url"] = git_url
    if source_branch:
        payload["source_branch"] = source_branch
    # Multi-tenancy (#925): carry the build's tenant so TFactory can scope its
    # side too. OPTIONAL additive metadata — absent when the spec was never
    # stamped (single-tenant deployments), never required by the ingest.
    # ponytail: optional additive metadata (see comment above); absent/corrupt
    # file just means no tenant_id gets attached.
    with contextlib.suppress(OSError, ValueError):
        tm = json.loads((spec_dir / "task_metadata.json").read_text())
        if tm.get("tenant_id"):
            payload["tenant_id"] = str(tm["tenant_id"])
    return payload


async def _httpx_poster(url: str, payload: dict, headers: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        return {
            "status": resp.status_code,
            "ok": resp.is_success,
            "body": resp.text[:2000],
        }


async def send_handoff(
    payload: dict,
    *,
    config: dict | None = None,
    poster: Poster | None = None,
) -> dict:
    """POST a handoff payload to TFactory. Never raises.

    Returns a JSON-able result: ``{"sent": bool, "reason": str|None, ...}``.
    ``reason`` is ``"not_configured"`` (no base URL), ``"http_error"`` (non-2xx),
    or ``"error"`` (transport exception).
    """
    config = config or tfactory_config()
    base_url = config.get("base_url")
    if not base_url:
        return {"sent": False, "reason": "not_configured"}

    url = base_url + (config.get("path") or _DEFAULT_PATH)
    headers = {"Content-Type": "application/json"}
    if config.get("token"):
        headers["Authorization"] = f"Bearer {config['token']}"

    poster = poster or _httpx_poster
    try:
        result = await poster(url, payload, headers)
    except Exception as exc:  # noqa: BLE001 — transport must never crash routing
        return {"sent": False, "reason": "error", "error": str(exc)[:300]}

    ok = bool(result.get("ok"))
    return {
        "sent": ok,
        "reason": None if ok else "http_error",
        "status": result.get("status"),
        "url": url,
    }


async def send_pr_attach(
    spec_dir: Path,
    spec_id: str,
    pr_number: int,
    repo_slug: str | None,
    *,
    poster: Poster | None = None,
) -> dict[str, Any]:
    """Tell TFactory the PR this build opened, so the verify verdict posts back.

    The verifying handoff is sent BEFORE the PR exists, so TFactory's source.json
    carries no PR number and its triager pr_comment step skips. Calling this the
    moment the PR opens back-fills it (POST /api/specs/{project}/{spec}/pr).
    Never raises — best-effort, never blocks the PR endgame (#964).
    """
    config = tfactory_config()
    base_url = config.get("base_url")
    if not base_url:
        return {"sent": False, "reason": "not_configured"}

    project_id = os.environ.get("TFACTORY_PROJECT_ID") or _aifactory_project_name(
        Path(spec_dir)
    )
    url = f"{base_url}/api/specs/{project_id}/{spec_id}/pr"
    headers = {"Content-Type": "application/json"}
    if config.get("token"):
        headers["Authorization"] = f"Bearer {config['token']}"
    payload: dict[str, Any] = {"pr_number": int(pr_number), "repo_slug": repo_slug}

    poster = poster or _httpx_poster
    try:
        result = await poster(url, payload, headers)
    except Exception as exc:  # noqa: BLE001 — transport must never crash the endgame
        return {"sent": False, "reason": "error", "error": str(exc)[:300]}
    ok = bool(result.get("ok"))
    return {
        "sent": ok,
        "reason": None if ok else "http_error",
        "status": result.get("status"),
    }


def wants_auto_handoff(spec_dir: Path) -> bool:
    """True if the task opted into auto-handover to TFactory on completion.

    Read from ``task_metadata.json`` (``auto_handover_tfactory``). Best-effort:
    a missing/unreadable file means "no".
    """
    try:
        meta = json.loads((Path(spec_dir) / "task_metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(meta, dict)
        and (meta.get("auto_handover_tfactory") or meta.get("autoHandoverTFactory"))
    )


async def maybe_auto_handoff_tfactory(spec_dir: Path, spec_id: str) -> dict:
    """On a task's terminal SUCCESS, hand the finished build to TFactory for
    testing — but only when the task opted in (``auto_handover_tfactory`` in
    task_metadata) AND TFactory is configured (``TFACTORY_BASE_URL``). The
    handoff carries the spec's requirements + PFactory/Task-Contract meta + the
    mutation ledger as evidence. Best-effort: never raises, never blocks task
    completion (#496).
    """
    spec_dir = Path(spec_dir)
    if not wants_auto_handoff(spec_dir):
        return {"sent": False, "reason": "not_requested"}
    # A build that wrote nothing has nothing to verify, and handing it off is
    # actively harmful: TFactory checks the branch out, finds no implementation,
    # generates tests against the missing feature, and reports a rigorous-looking
    # negative for code that was never written (#984 — same hollow-verify shape
    # as TFactory #729, reached from the build side). Observed live: the branch
    # for spec 005 was the same commit as its base, the build still reported
    # success in four minutes, and verify was handed a tree with no build in it.
    # Only an explicit 0 blocks — `None` means unknowable, and refusing a build
    # we merely could not measure would be worse than the bug.
    if build_commit_count(spec_dir, spec_id) == 0:
        return {"sent": False, "reason": "empty_build"}
    try:
        payload = build_ingest_payload(spec_dir, spec_id)
        result = await send_handoff(payload)
    except Exception as exc:  # noqa: BLE001 — must never break task completion
        return {"sent": False, "reason": "error", "error": str(exc)[:300]}
    # Record a local marker so the UI / operator can see the handoff outcome.
    with contextlib.suppress(OSError):
        (spec_dir / "tfactory_handoff.json").write_text(json.dumps(result, indent=2))
    return result
