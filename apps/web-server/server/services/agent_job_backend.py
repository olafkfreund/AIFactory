"""PR review and clarification agent sessions as Kubernetes Jobs (A4, #1932).

Reuses the spec Job substrate: ``SpecJobProcess`` is the Process view, so the
callers keep their subprocess-shaped code. Off unless ``AIFACTORY_REVIEW_BACKEND=kubejob``.
A dispatch failure refuses the session; it never falls back to the in-pod spawn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from factory_common.logsafe import sanitize_log

from . import build_backend
from .job_state_store import store_enabled
from .spec_job_backend import _LIVE_SPEC_JOBS, SpecJobProcess

_log = logging.getLogger(__name__)
_ENV_REVIEW_BACKEND = "AIFACTORY_REVIEW_BACKEND"
_DEADLINE_SECONDS = 1800
_LOCAL_DEADLINE = _DEADLINE_SECONDS + 300.0  # stop polling shortly after the Job's own
CLARIFICATION_MAX_BYTES = 64 << 10
_REVIEW_FILE_MAX_BYTES = 8 << 20


def review_kubejob_enabled() -> bool:
    value = os.environ.get(_ENV_REVIEW_BACKEND, "").strip().lower()
    if value in {"", "subprocess"}:
        return False
    if value == "kubejob":
        return True
    raise build_backend.BuildBackendUnavailableError(
        f"Invalid {_ENV_REVIEW_BACKEND}; valid values: subprocess, kubejob"
    )


def build_agent_job_manifest(
    *,
    job_id: str,
    argv: list[str],
    workspace_uri: str | None,
    extra_env: dict[str, str],
    models: list[str],
) -> dict[str, Any]:
    from core.job_dispatch import (  # noqa: PLC0415 - backend path is set at startup
        DEFAULT_NIX_IMAGE,
        JobSpec,
        build_job_manifest,
    )

    service_account = (
        os.environ.get(
            build_backend._ENV_SERVICE_ACCOUNT, build_backend._DEFAULT_SERVICE_ACCOUNT
        ).strip()
        or build_backend._DEFAULT_SERVICE_ACCOUNT
    )
    spec = JobSpec(
        service="aifactory",
        job_id=job_id,
        commands=[shlex.join(argv)],
        image=build_backend._resolve_build_image(DEFAULT_NIX_IMAGE),
        service_account=service_account,
        namespace=build_backend._dispatch_namespace(),
        data_pvc=None,
        worktree_subpath=None,
        deadline_seconds=_DEADLINE_SECONDS,
        nix_develop=False,
        extra_env=extra_env,
        workspace_uri=workspace_uri,
        automount_service_account_token=False,
    )
    cred_files = build_backend.cred_files_for_models(models)
    return build_backend._inject_build_path(
        build_backend._inject_seed_creds(build_job_manifest(spec), cred_files)
    )


def claude_oauth_token() -> str | None:
    """The pooled Claude login for a Job, or None to let the Job's seeded creds apply."""
    from .insights_providers import get_provider  # noqa: PLC0415

    try:
        return get_provider("claude")._resolve_claude_token()[0] or None
    except Exception:  # noqa: BLE001 - optional, as for the in-pod clarification
        _log.warning(
            "[agent_job_backend] could not resolve a Claude token", exc_info=True
        )
        return None


def _require_prerequisites() -> None:
    if not store_enabled():
        raise build_backend.BuildBackendUnavailableError(
            f"{_ENV_REVIEW_BACKEND}=kubejob requires DATABASE_URL, which is not set"
        )


async def _create_job(manifest: dict[str, Any], batch: Any) -> tuple[Any, str, str]:
    namespace = manifest["metadata"]["namespace"]
    job_name = manifest["metadata"]["name"]
    if batch is None:
        batch = await build_backend.KubeJobBuildBackend._batch_api()
    try:
        await batch.create_namespaced_job(namespace, manifest)
    except Exception:
        api = getattr(batch, "api_client", None)
        if api is not None:
            with contextlib.suppress(Exception):
                await api.close()
        raise
    _LIVE_SPEC_JOBS.add(job_name)
    _log.info(
        "[agent_job_backend] dispatched Job %s/%s",
        sanitize_log(namespace),
        sanitize_log(job_name),
    )
    return batch, namespace, job_name


def _prepare_review_workspace(
    project_path: Path, pr_number: int, followup: bool, job_id: str
) -> str:
    from core.artifact_store import (  # noqa: PLC0415 - avoid boto3 at import time
        ArtifactRef,
        ArtifactStore,
        pack_workspace,
    )

    clone = project_path / ".aifactory/worktrees/reviews" / job_id
    try:
        clone.parent.mkdir(parents=True, exist_ok=True)
        build_backend._git(
            ["clone", "--local", "--no-hardlinks", str(project_path), str(clone)]
        )
        if followup:
            # The follow-up loads the previous result; ship only that one file.
            prev = (
                project_path / ".aifactory/github/pr" / f"review_{int(pr_number)}.json"
            )
            if not prev.is_symlink() and prev.is_file():
                if prev.stat().st_size > _REVIEW_FILE_MAX_BYTES:
                    raise build_backend.BuildBackendUnavailableError(
                        "Previous review result is too large"
                    )
                if not isinstance(json.loads(prev.read_bytes()), dict):
                    raise build_backend.BuildBackendUnavailableError(
                        "Previous review result is not a JSON object"
                    )
                dest = clone / ".aifactory/github/pr"
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(prev, dest / prev.name)
        return str(
            pack_workspace(
                ArtifactStore(),
                ArtifactRef(service="aifactory", job_id=job_id, role="workspace"),
                clone,
            )
        )
    finally:
        if clone.exists():
            shutil.rmtree(clone)


async def dispatch_pr_review(
    *,
    project_path: Path,
    pr_number: int,
    followup: bool,
    project_env: dict[str, str],
    oauth_token: str | None,
    batch: Any = None,
) -> SpecJobProcess:
    """Run ``runner.py review-pr N`` in a Job; success fetches only review_{N}.json."""
    from core.workspace_fetch import (  # noqa: PLC0415
        PR_REVIEW_FETCH_ID_ENV,
        fetch_pr_review,
    )

    from .job_s3_scope import scope_job_s3  # noqa: PLC0415

    _require_prerequisites()
    fetch_id = uuid4().hex
    job_id = f"pr{int(pr_number)}-review-{fetch_id[:8]}"
    uri = await asyncio.to_thread(
        _prepare_review_workspace, project_path, pr_number, followup, job_id
    )
    env = build_backend.build_job_env(oauth_token, project_path)
    env.update(project_env)
    env[PR_REVIEW_FETCH_ID_ENV] = fetch_id
    # After every env merge, so none can restore the bucket-wide key.
    await asyncio.to_thread(scope_job_s3, env, [(None, job_id), (None, fetch_id)])
    runner = (
        Path(build_backend._resolve_run_py_path()).parent / "runners/github/runner.py"
    )
    argv = [
        "python",
        str(runner),
        "--project",
        "/work",
        "followup-review-pr" if followup else "review-pr",
        str(int(pr_number)),
    ]
    manifest = build_agent_job_manifest(
        job_id=job_id,
        argv=argv,
        workspace_uri=uri,
        extra_env=env,
        models=["sonnet", "opus"],
    )
    batch, namespace, job_name = await _create_job(manifest, batch)
    cancel = threading.Event()

    async def finish() -> int:
        ok = await asyncio.to_thread(
            fetch_pr_review, project_path, fetch_id, pr_number, cancel
        )
        return 0 if ok else 1

    async def existing_batch() -> object:
        return batch

    return SpecJobProcess(
        namespace,
        job_name,
        batch_factory=existing_batch,
        finish=finish,
        cancel=cancel,
        deadline=_LOCAL_DEADLINE,
    )


async def dispatch_clarification(
    *, prompt: str, oauth_token: str | None, batch: Any = None
) -> SpecJobProcess:
    """Run ``claude --print`` in a workspace-less Job; the answer is the Job's log."""
    _require_prerequisites()
    env = build_backend.build_job_env(oauth_token)
    # Nothing to read or write remotely: no GitHub or object-store credential.
    for name in [n for n in env if n.startswith("S3_")] + ["GITHUB_TOKEN", "GH_TOKEN"]:
        env.pop(name, None)
    manifest = build_agent_job_manifest(
        job_id=f"clarify-{uuid4().hex[:8]}",
        argv=["claude", "--print", "--model", "haiku", prompt],
        workspace_uri=None,
        extra_env=env,
        models=["haiku"],
    )
    batch, namespace, job_name = await _create_job(manifest, batch)

    async def finish() -> int:
        return 0

    async def existing_batch() -> object:
        return batch

    return SpecJobProcess(
        namespace,
        job_name,
        batch_factory=existing_batch,
        finish=finish,
        deadline=_LOCAL_DEADLINE,
    )


async def read_clarification(
    proc: SpecJobProcess, *, timeout: float = 60.0
) -> str | None:
    """The Job's stdout, or None when it failed, was cut, or ran out of time."""
    try:
        # The feed caps the buffer (1 MiB), so reading to EOF is bounded.
        data = await asyncio.wait_for(proc.stdout.read(), timeout=timeout)
        failed = await asyncio.wait_for(proc.wait(), timeout=5.0)
        if failed or proc.truncated or len(data) > CLARIFICATION_MAX_BYTES:
            return None
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - the caller skips on None
        _log.warning("[agent_job_backend] clarification Job gave no answer")
        return None
    finally:
        proc.terminate()  # no-op once the Job has finished
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
