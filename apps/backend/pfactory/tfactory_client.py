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

import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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


def _build_worktree(spec_dir: Path, spec_id: str) -> Path:
    """The build worktree for a spec: <project>/.aifactory/worktrees/tasks/<spec_id>."""
    p = Path(spec_dir).resolve()
    # spec_dir == <project>/.aifactory/specs/<spec_id>
    project_dir = p.parents[2] if len(p.parents) >= 3 else p.parent
    return project_dir / ".aifactory" / "worktrees" / "tasks" / spec_id


def _git_info_and_push(spec_dir: Path, spec_id: str) -> tuple[str | None, str | None]:
    """Return ``(git_url, source_branch)`` for the build worktree, pushing the branch
    to origin so TFactory (separate PVC) can fetch the built code.

    Best-effort and never raises: returns ``(None, None)`` if the worktree/remote is
    missing or git fails — the handoff then degrades gracefully.
    """
    import subprocess

    wt = _build_worktree(spec_dir, spec_id)
    if not wt.is_dir():
        return None, None

    def _git(args: list[str]) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(wt), capture_output=True, text=True, timeout=60
        ).stdout.strip()

    try:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
        url = _git(["remote", "get-url", "origin"])
        if not branch or not url:
            return None, None
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        push_url = url
        if token and url.startswith("https://github.com/"):
            push_url = url.replace("https://", f"https://x-access-token:{token}@", 1)
        # Push the build branch so TFactory can clone/fetch it. Best-effort.
        subprocess.run(
            ["git", "push", push_url, f"HEAD:{branch}"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return url, branch
    except Exception:  # noqa: BLE001 - handoff prep must never break the build
        return None, None


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
    if contract:
        payload["contract"] = contract
    # PARR seam: hand TFactory the repo + the build branch so it can fetch the
    # ACTUAL built code (separate PVC). Pushes the branch (best-effort) and lets
    # TFactory self-register the project from git_url when it isn't pre-registered.
    git_url, source_branch = _git_info_and_push(spec_dir, spec_id)
    if git_url:
        payload["git_url"] = git_url
    if source_branch:
        payload["source_branch"] = source_branch
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
    try:
        payload = build_ingest_payload(spec_dir, spec_id)
        result = await send_handoff(payload)
    except Exception as exc:  # noqa: BLE001 — must never break task completion
        return {"sent": False, "reason": "error", "error": str(exc)[:300]}
    # Record a local marker so the UI / operator can see the handoff outcome.
    try:
        (spec_dir / "tfactory_handoff.json").write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    return result
