"""Deploy endgame — real CI/CD deploy stage between build and TFactory verify.

On a clean build (and only when ``AIFACTORY_AUTO_DEPLOY`` is enabled) this:

  1. writes the DETERMINISTIC deploy artifacts into the worktree
     (``deploy_templates.deploy_files`` → ``.github/workflows/{deploy,destroy}.yml``
     + ``infra/main.tf``) — so teardown ships with every deploy,
  2. sets the AWS keys as GitHub repo secrets (``gh secret set`` — gh seals them),
  3. commits + pushes the branch,
  4. triggers the ``deploy.yml`` workflow and watches the run it created
     (matched by head sha, never "latest"),
  5. captures the live App Runner URLs from the ``deployed-urls`` artifact,
  6. writes ``deploy_result.json`` into the spec dir for the TFactory handoff (#547).

Cost guard: ``teardown()`` fires ``destroy.yml``; the caller (TFactory post-verify
hook + a safety-net timer) ensures it always runs. Everything goes through an
injectable ``runner`` so the chain is unit-testable without git/gh/network.
Flag defaults OFF — inert until explicitly enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from . import deploy_templates
from .pr_endgame import CmdResult, Runner, _default_runner, _project_env

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_POLL_INTERVAL_SECONDS = 20
_MAX_POLL_MINUTES = 25
_DEPLOY_WF = "deploy.yml"
_DESTROY_WF = "destroy.yml"
_DEFAULT_REGION = "eu-west-1"

# Secret names consumed by the generated workflows.
_SECRET_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def _flag(project_path: Path | None, key: str, default: bool = False) -> bool:
    """project .aifactory/.env → global env → default."""
    val = _project_env(project_path, key)
    if val is None:
        val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in _TRUTHY


def is_deploy_enabled(project_path: Path | None) -> bool:
    """True when AIFACTORY_AUTO_DEPLOY is set (default OFF). Inert otherwise."""
    return _flag(project_path, "AIFACTORY_AUTO_DEPLOY", default=False)


def deploy_region(project_path: Path | None) -> str:
    return _project_env(project_path, "AWS_DEFAULT_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", _DEFAULT_REGION
    )


def _state_bucket(project_path: Path | None) -> str:
    return (
        _project_env(project_path, "AIFACTORY_TF_STATE_BUCKET")
        or os.environ.get("AIFACTORY_TF_STATE_BUCKET", "")
    )


def parse_repo(repo: str) -> tuple[str, str] | None:
    """'owner/name' or a github URL → (owner, name)."""
    if not repo:
        return None
    r = repo.strip().removesuffix(".git")
    if r.startswith("http"):
        parts = [p for p in r.split("/") if p]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return None
    if "/" in r:
        owner, _, name = r.partition("/")
        return owner, name
    return None


def detect_services(worktree: Path) -> list[str]:
    """Infer deployable service names from the worktree.

    A 'service' is a top-level dir containing a Dockerfile or a FastAPI entry
    (``main.py``/``app.py``). Falls back to a single 'app' service so a deploy is
    always shaped (deploy_templates also guarantees >=1).
    """
    worktree = Path(worktree)
    svcs: list[str] = []
    try:
        for child in sorted(worktree.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if child.name in {"infra", "tests", "docs", "node_modules", "venv"}:
                continue
            has_docker = (child / "Dockerfile").exists()
            has_app = (child / "main.py").exists() or (child / "app.py").exists()
            if has_docker or has_app:
                svcs.append(child.name)
    except OSError:
        pass
    if not svcs and (worktree / "Dockerfile").exists():
        svcs = ["app"]
    return svcs


def write_deploy_files(
    worktree: Path,
    services: list[str],
    *,
    spec_id: str,
    region: str,
    state_bucket: str,
) -> list[str]:
    """Write the deterministic deploy artifacts into the worktree. Returns the
    relative paths written (so the caller can `git add` them)."""
    files = deploy_templates.deploy_files(
        services, spec_id=spec_id, region=region, state_bucket=state_bucket
    )
    written: list[str] = []
    for rel, content in files.items():
        dest = Path(worktree) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def set_repo_secrets(owner: str, repo: str, creds: dict[str, str], *, runner: Runner) -> bool:
    """Set AWS keys as repo Actions secrets via `gh secret set` (gh seals them).
    Secret VALUES never logged — only names."""
    full = f"{owner}/{repo}"
    for key in _SECRET_KEYS:
        val = creds.get(key)
        if not val:
            logger.error("deploy: missing credential %s — cannot set repo secret", key)
            return False
        r = runner(["gh", "secret", "set", key, "--repo", full, "--body", val], None)
        if not r.ok:
            logger.error("deploy: gh secret set %s failed on %s: %s", key, full, r.err[:200])
            return False
        logger.info("deploy: set repo secret %s on %s", key, full)
    return True


def commit_and_push(worktree: str, branch: str, paths: list[str], *, runner: Runner) -> bool:
    """gh auth setup-git, stage the deploy files, commit, push the branch."""
    runner(["gh", "auth", "setup-git"], worktree)
    add = runner(["git", "add", *paths], worktree)
    if not add.ok:
        logger.error("deploy: git add failed: %s", add.err[:200])
        return False
    # Nothing to commit is fine (files already present) — proceed to push.
    runner(["git", "commit", "-m", "ci: factory deploy + teardown workflows"], worktree)
    push = runner(["git", "push", "origin", f"HEAD:{branch}"], worktree)
    if not push.ok:
        logger.error("deploy: git push failed: %s", push.err[:200])
        return False
    return True


def trigger_workflow(owner: str, repo: str, branch: str, workflow: str, *, runner: Runner) -> bool:
    full = f"{owner}/{repo}"
    r = runner(["gh", "workflow", "run", workflow, "--ref", branch, "--repo", full], None)
    if not r.ok:
        logger.error("deploy: gh workflow run %s failed: %s", workflow, r.err[:200])
    return r.ok


def latest_run_id(owner: str, repo: str, head_sha: str, workflow: str, *, runner: Runner) -> int | None:
    """Find the run for OUR head sha (never blindly 'latest' — avoids capturing a
    concurrent push's run)."""
    full = f"{owner}/{repo}"
    r = runner(
        ["gh", "run", "list", "--workflow", workflow, "--repo", full,
         "--json", "databaseId,headSha,status", "--limit", "20"],
        None,
    )
    if not r.ok:
        return None
    try:
        runs = json.loads(r.out or "[]")
    except json.JSONDecodeError:
        return None
    for run in runs:
        if not head_sha or run.get("headSha", "").startswith(head_sha[:12]) or head_sha.startswith(run.get("headSha", "x")[:12]):
            return run.get("databaseId")
    return runs[0].get("databaseId") if runs else None


async def watch_workflow(owner: str, repo: str, run_id: int, *, runner: Runner,
                         poll: int = _POLL_INTERVAL_SECONDS, max_minutes: int = _MAX_POLL_MINUTES) -> dict:
    """Poll a run until terminal or timeout. Returns {status, conclusion, run_id}."""
    full = f"{owner}/{repo}"
    deadline = max(1, int(max_minutes * 60 / max(poll, 1)))
    for _ in range(deadline):
        r = runner(["gh", "run", "view", str(run_id), "--repo", full,
                    "--json", "status,conclusion"], None)
        if r.ok:
            try:
                d = json.loads(r.out or "{}")
            except json.JSONDecodeError:
                d = {}
            if d.get("status") == "completed":
                return {"status": "completed", "conclusion": d.get("conclusion"), "run_id": run_id}
        await asyncio.sleep(poll)
    return {"status": "timeout", "conclusion": None, "run_id": run_id}


def capture_deployed_url(owner: str, repo: str, run_id: int, dest_dir: str, *, runner: Runner) -> dict | None:
    """Download the deployed-urls artifact → {service: https_url}."""
    full = f"{owner}/{repo}"
    r = runner(["gh", "run", "download", str(run_id), "--repo", full,
                "--name", "deployed-urls", "--dir", dest_dir], None)
    if not r.ok:
        logger.error("deploy: artifact download failed: %s", r.err[:200])
        return None
    p = Path(dest_dir) / "deployed_urls.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def teardown(owner: str, repo: str, branch: str, *, runner: Runner | None = None) -> bool:
    """Fire destroy.yml (cost guard). Best-effort, idempotent.

    NOTE (verified live): ``gh workflow run destroy.yml`` 404s unless destroy.yml
    is on the repo's DEFAULT branch — GitHub only dispatches default-branch
    workflows. So this dispatch is reliable ONLY when a persistent teardown
    workflow lives on ``main``; on a feature-branch-only setup it will fail. The
    DURABLE cost guards therefore do not depend on it: (1) the scheduled sweeper
    destroys anything tagged ``factory-ephemeral`` older than 1h, and (2) the
    operator/conductor can ``terraform destroy`` directly against the shared S3
    state. Keep this as the fast-path nudge, not the sole guarantee.
    """
    runner = runner or _default_runner
    return trigger_workflow(owner, repo, branch, _DESTROY_WF, runner=runner)


async def run_deploy_endgame(
    *,
    spec_dir: Path,
    spec_id: str,
    worktree: str,
    branch: str,
    repo: str,
    creds: dict[str, str],
    region: str = _DEFAULT_REGION,
    state_bucket: str = "",
    runner: Runner | None = None,
) -> dict:
    """Drive the deploy stage. Never raises. Writes spec_dir/deploy_result.json.

    Returns a JSON-able result dict; ``deployed`` true only when the workflow
    succeeded and live URLs were captured.
    """
    runner = runner or _default_runner
    spec_dir = Path(spec_dir)
    result: dict = {"deployed": False, "reason": None, "repo": repo, "branch": branch}

    parsed = parse_repo(repo)
    if not parsed:
        result["reason"] = "unresolvable_repo"
        _write_result(spec_dir, result)
        return result
    owner, name = parsed

    if not creds.get("AWS_ACCESS_KEY_ID") or not creds.get("AWS_SECRET_ACCESS_KEY"):
        result["reason"] = "no_aws_creds"
        _write_result(spec_dir, result)
        return result

    try:
        services = detect_services(Path(worktree))
        written = write_deploy_files(
            Path(worktree), services, spec_id=spec_id, region=region, state_bucket=state_bucket
        )
        result["services"] = services
        if not set_repo_secrets(owner, name, creds, runner=runner):
            result["reason"] = "secret_set_failed"
            _write_result(spec_dir, result)
            return result
        if not commit_and_push(worktree, branch, written, runner=runner):
            result["reason"] = "push_failed"
            _write_result(spec_dir, result)
            return result
        head = runner(["git", "rev-parse", "HEAD"], worktree)
        head_sha = head.out.strip() if head.ok else ""
        result["head_sha"] = head_sha
        # The push above ALREADY triggers deploy.yml via its `on: push` filter
        # (auto-claude/** + <spec>**) — verified live. workflow_dispatch is only a
        # best-effort nudge for repos where the workflow also sits on the default
        # branch (GitHub 404s dispatch otherwise), so its failure is NOT fatal.
        trigger_workflow(owner, name, branch, _DEPLOY_WF, runner=runner)
        # Find the run our push created (poll briefly — the run may take a few
        # seconds to register).
        run_id = None
        for _ in range(6):
            run_id = latest_run_id(owner, name, head_sha, _DEPLOY_WF, runner=runner)
            if run_id is not None:
                break
            await asyncio.sleep(5)
        if run_id is None:
            result["reason"] = "run_not_found"
            _write_result(spec_dir, result)
            return result
        result["run_id"] = run_id
        watched = await watch_workflow(owner, name, run_id, runner=runner)
        result["conclusion"] = watched.get("conclusion")
        if watched.get("conclusion") != "success":
            result["reason"] = f"workflow_{watched.get('conclusion') or watched.get('status')}"
            # Tear down any partial infra immediately on a failed/timed-out deploy.
            teardown(owner, name, branch, runner=runner)
            _write_result(spec_dir, result)
            return result
        urls = capture_deployed_url(owner, name, run_id, str(spec_dir), runner=runner) or {}
        result["urls"] = urls
        # Primary deployed_url = first service URL (TFactory base target).
        result["deployed_url"] = next(iter(urls.values()), None)
        result["deployed"] = bool(result["deployed_url"])
        if not result["deployed"]:
            result["reason"] = "no_url_captured"
            teardown(owner, name, branch, runner=runner)
    except Exception as exc:  # noqa: BLE001 — deploy must never break completion
        logger.error("deploy_endgame failed for %s: %r", spec_id, exc, exc_info=exc)
        result["reason"] = f"error: {exc}"[:200]
        try:
            teardown(owner, name, branch, runner=runner)
        except Exception:
            pass
    _write_result(spec_dir, result)
    return result


def _write_result(spec_dir: Path, result: dict) -> None:
    try:
        (Path(spec_dir) / "deploy_result.json").write_text(json.dumps(result, indent=2))
    except OSError:
        pass
