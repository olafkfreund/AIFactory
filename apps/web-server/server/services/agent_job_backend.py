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
import subprocess
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
_ENV_CORRECTION_BACKEND = "AIFACTORY_CORRECTION_BACKEND"
_CORRECTION_SPEC_FILE_MAX_BYTES = 1 << 20
_CORRECTION_RESULT_FILE = "handback_job_result.json"
_CORRECTION_WAITERS: set[asyncio.Task[None]] = set()


def _kubejob_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in {"", "subprocess"}:
        return False
    if value == "kubejob":
        return True
    raise build_backend.BuildBackendUnavailableError(
        f"Invalid {name}; valid values: subprocess, kubejob"
    )


def review_kubejob_enabled() -> bool:
    return _kubejob_flag(_ENV_REVIEW_BACKEND)


def correction_kubejob_enabled() -> bool:
    """QA apply-correction runs as a Job (#2116); off by default."""
    return _kubejob_flag(_ENV_CORRECTION_BACKEND)


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

    service_account = build_backend._job_service_account()
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
        ttl_seconds=build_backend.job_ttl_seconds(),
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
        provider: Any = get_provider("claude")
        return provider._resolve_claude_token()[0] or None
    except Exception:  # noqa: BLE001 - optional, as for the in-pod clarification
        _log.warning(
            "[agent_job_backend] could not resolve a Claude token", exc_info=True
        )
        return None


def _require_prerequisites(flag: str = _ENV_REVIEW_BACKEND) -> None:
    if not store_enabled():
        raise build_backend.BuildBackendUnavailableError(
            f"{flag}=kubejob requires DATABASE_URL, which is not set"
        )


async def _create_job(manifest: dict[str, Any], batch: Any) -> tuple[Any, str, str]:
    namespace = manifest["metadata"]["namespace"]
    job_name = manifest["metadata"]["name"]
    if batch is None:
        batch = await build_backend.KubeJobBuildBackend._batch_api()
    try:
        await batch.create_namespaced_job(namespace, manifest)
    except BaseException:  # incl. cancellation: the API may have accepted the Job
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                batch.delete_namespaced_job(
                    job_name, namespace, propagation_policy="Background"
                ),
                timeout=10,
            )
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


def _has_symlink(root: Path, rel: Path) -> bool:
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _restore_origin(project_path: Path, clone: Path) -> None:
    """Point the clone's origin at the project's GitHub remote, without credentials."""
    from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

    url = build_backend._git(
        ["-C", str(project_path), "remote", "get-url", "origin"], check=False
    ).stdout.strip()
    if not url:
        return
    parts = urlsplit(url)
    if parts.scheme in {"http", "https"} and "@" in parts.netloc:
        url = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    build_backend._git(["-C", str(clone), "remote", "set-url", "origin", url])


def _prepare_review_workspace(
    project_path: Path, pr_number: int, followup: bool, job_id: str
) -> str:
    from core.artifact_store import (  # noqa: PLC0415 - avoid boto3 at import time
        ArtifactRef,
        ArtifactStore,
        pack_workspace,
    )

    if _has_symlink(project_path, Path(".aifactory/worktrees/reviews")):
        raise build_backend.BuildBackendUnavailableError(
            "Review worktree directory contains a symlink"
        )
    clone = project_path / ".aifactory/worktrees/reviews" / job_id
    try:
        clone.parent.mkdir(parents=True, exist_ok=True)
        build_backend._git(
            ["clone", "--local", "--no-hardlinks", str(project_path), str(clone)]
        )
        _restore_origin(project_path, clone)  # the runner finds the repo via origin
        if followup:
            # The follow-up loads the previous result; ship only that one file.
            rel = Path(".aifactory/github/pr")
            prev = project_path / rel / f"review_{int(pr_number)}.json"
            # A symlink in the project or the fresh clone must not redirect the copy.
            if _has_symlink(project_path, rel) or _has_symlink(clone, rel):
                raise build_backend.BuildBackendUnavailableError(
                    "Review directory contains a symlink"
                )
            if (clone / rel / prev.name).is_symlink():
                raise build_backend.BuildBackendUnavailableError(
                    "Review file is a symlink"
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
    batch, namespace, job_name = await _create_job(manifest, None)
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
    from server.utils.subprocess_env import github_names  # noqa: PLC0415

    drop = {*github_names(), "GITHUB_TOKEN", "GH_TOKEN"}
    for name in [
        n for n in env if n in drop or n.startswith(("S3_", "GITHUB_", "GH_"))
    ]:
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
    proc: SpecJobProcess, *, max_wait: float = 60.0
) -> str | None:
    """The Job's stdout, or None when it failed, was cut, too long, or out of time."""
    buf = bytearray()

    async def drain() -> bool:
        while chunk := await proc.stdout.read(4096):
            buf.extend(chunk)
            if len(buf) > CLARIFICATION_MAX_BYTES:
                return False
        return True

    try:
        complete = await asyncio.wait_for(drain(), timeout=max_wait)
        if not complete or proc.truncated:
            return None
        if await asyncio.wait_for(proc.wait(), timeout=5.0):  # non-zero: Job failed
            return None
        return buf.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - the caller skips on None
        _log.warning("[agent_job_backend] clarification Job gave no answer")
        return None
    finally:
        proc.terminate()  # no-op once the Job has finished
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)


def _prepare_correction_workspace(spec_dir: Path, job_id: str) -> str:
    """Pack a clone of the project (committed state) plus the spec's top-level files."""
    from core.artifact_store import (  # noqa: PLC0415 - avoid boto3 at import time
        ArtifactRef,
        ArtifactStore,
        pack_workspace,
    )

    project = spec_dir.parent.parent.parent
    spec_rel = Path(".aifactory/specs") / spec_dir.name
    if spec_dir.parent.name != "specs" or _has_symlink(project, spec_rel):
        raise build_backend.BuildBackendUnavailableError("Spec directory is not safe")
    if _has_symlink(project, Path(".aifactory/worktrees/corrections")):
        raise build_backend.BuildBackendUnavailableError(
            "Correction worktree directory contains a symlink"
        )
    sources = [f for f in spec_dir.iterdir() if f.is_file() and not f.is_symlink()]
    if any(f.stat().st_size > _CORRECTION_SPEC_FILE_MAX_BYTES for f in sources):
        raise build_backend.BuildBackendUnavailableError("A spec file is too large")
    clone = project / ".aifactory/worktrees/corrections" / job_id
    handback = False
    packed = False
    try:
        clone.parent.mkdir(parents=True, exist_ok=True)
        build_backend._git(
            ["clone", "--local", "--no-hardlinks", str(project), str(clone)]
        )
        _restore_origin(project, clone)
        with contextlib.suppress(OSError, ValueError):
            receipt = json.loads((spec_dir / "handback_received.json").read_text())
            handback = bool(receipt.get("tfactory_task_id"))
        if handback:
            from trusted_plan import _safe_branch  # noqa: PLC0415

            from server.services.task_branch import (  # noqa: PLC0415
                current_branch,
                resolve_task_branch_fetching,
            )

            branch, reason = resolve_task_branch_fetching(
                worktree_path=project / ".aifactory/worktrees/tasks" / spec_dir.name,
                project_path=project,
                spec_id=spec_dir.name,
                base_branch=current_branch(project),
            )
            if branch is None or not _safe_branch(branch):
                raise build_backend.BuildBackendUnavailableError(
                    reason or "Task branch missing"
                )
            build_backend._git(["-C", str(clone), "fetch", "origin", branch])
            build_backend._git(
                ["-C", str(clone), "checkout", "-B", branch, "FETCH_HEAD"]
            )
        if _has_symlink(clone, spec_rel):
            raise build_backend.BuildBackendUnavailableError(
                "Spec directory in the clone contains a symlink"
            )
        dest = clone / spec_rel
        dest.mkdir(parents=True, exist_ok=True)
        for src in sources:
            (dest / src.name).unlink(
                missing_ok=True
            )  # a cloned symlink, not its target
            shutil.copyfile(src, dest / src.name)
        uri = str(
            pack_workspace(
                ArtifactStore(),
                ArtifactRef(service="aifactory", job_id=job_id, role="workspace"),
                clone,
            )
        )
        packed = True
        return uri
    finally:
        if clone.exists() and (not handback or not packed):
            shutil.rmtree(clone)


def _correction_env(
    oauth_token: str | None, project: Path, ids: list[str]
) -> dict[str, str]:
    from server.utils.subprocess_env import github_names  # noqa: PLC0415

    from .job_s3_scope import scope_job_s3  # noqa: PLC0415

    env = build_backend.build_job_env(oauth_token, project)
    drop = {*github_names(), "GITHUB_TOKEN", "GH_TOKEN"}
    for name in [n for n in env if n in drop or n.startswith(("GITHUB_", "GH_"))]:
        env.pop(name, None)
    scope_job_s3(env, [(None, i) for i in ids])
    # scope_job_s3 is a silent no-op when scoping is off: this Job must never
    # hold the bucket-wide key, because its output is a code edit.
    if (
        not env.get("AWS_ACCESS_KEY_ID")
        or env.get("S3_ACCESS_KEY")
        or env.get("S3_SECRET_KEY")
    ):
        raise build_backend.BuildBackendUnavailableError(
            "scoped Job S3 credentials are required for a correction Job"
        )
    return env


async def _await_correction(
    proc: SpecJobProcess, spec_dir: Path, clone: Path | None = None
) -> None:
    """Record the Job's outcome, then run the assertion guard on the applied tree."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from qa.correction import check_fix_cycle_assertions  # noqa: PLC0415

    try:
        code = await proc.wait()
    except Exception:  # noqa: BLE001 - recorded as a failure below
        _log.exception("[agent_job_backend] correction Job wait failed")
        code = -1
    result = {
        "status": "applied" if code == 0 else "failed",
        "job": proc.job_name,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    with contextlib.suppress(OSError):
        (spec_dir / _CORRECTION_RESULT_FILE).write_text(json.dumps(result))
    _log.info(
        "[agent_job_backend] correction Job %s %s",
        sanitize_log(proc.job_name),
        result["status"],
    )
    await asyncio.to_thread(check_fix_cycle_assertions, spec_dir, clone)
    if clone is not None and clone.exists():
        shutil.rmtree(clone)


async def dispatch_correction_fixer(spec_dir: Path | str) -> dict[str, Any]:
    """Run the QA fixer for an apply-correction as a Job; the fix returns as a file set.

    The ``fixer_fn`` the route hands to ``apply_correction`` when
    ``AIFACTORY_CORRECTION_BACKEND=kubejob``. A failure raises
    ``BuildBackendUnavailableError``; it never falls back to the in-pod fixer.
    """
    from core.workspace_fetch import (  # noqa: PLC0415
        CORRECTION_FETCH_ID_ENV,
        fetch_correction,
    )

    spec = Path(spec_dir)
    project = spec.parent.parent.parent
    _require_prerequisites(_ENV_CORRECTION_BACKEND)
    fetch_id = uuid4().hex
    job_id = f"correction-{fetch_id[:8]}"
    clone = project / ".aifactory/worktrees/corrections" / job_id
    applied_paths: list[str] = []
    try:
        env = await asyncio.to_thread(
            _correction_env,
            await asyncio.to_thread(claude_oauth_token),
            project,
            [job_id, fetch_id],
        )
        env[CORRECTION_FETCH_ID_ENV] = fetch_id
        uri = await asyncio.to_thread(_prepare_correction_workspace, spec, job_id)
        if clone.exists():
            from qa.correction import snapshot_correction_workspace  # noqa: PLC0415

            await asyncio.to_thread(snapshot_correction_workspace, spec, clone)
    except build_backend.BuildBackendUnavailableError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise build_backend.BuildBackendUnavailableError(
            f"could not prepare the correction workspace ({type(exc).__name__})"
        ) from None
    script = Path(build_backend._resolve_run_py_path()).parent / "qa/correction_job.py"
    try:
        manifest = build_agent_job_manifest(
            job_id=job_id,
            argv=["python", str(script), "--project", "/work", "--spec-id", spec.name],
            workspace_uri=uri,
            extra_env=env,
            models=["sonnet", "opus"],
        )
        batch, namespace, job_name = await _create_job(manifest, None)
    except Exception as exc:  # noqa: BLE001 - an API error is a refusal, not a 500
        if clone.exists():
            shutil.rmtree(clone)
        raise build_backend.BuildBackendUnavailableError(
            f"could not create the correction Job ({type(exc).__name__})"
        ) from None
    cancel = threading.Event()

    async def finish() -> int:
        if clone.exists():
            ok = await asyncio.to_thread(
                fetch_correction, clone, fetch_id, cancel, applied_paths
            )
            if ok:
                from qa.correction import (  # noqa: PLC0415
                    check_fix_cycle_assertions,
                    publish_correction,
                )

                branch = build_backend._git(
                    ["-C", str(clone), "symbolic-ref", "--short", "HEAD"]
                ).stdout.strip()
                report = await asyncio.to_thread(
                    check_fix_cycle_assertions, spec, clone
                )
                ok = report["ok"] and await publish_correction(
                    spec, clone, branch, applied_paths
                )
        else:
            ok = await asyncio.to_thread(fetch_correction, project, fetch_id, cancel)
        return 0 if ok else 1

    async def existing_batch() -> object:
        return batch

    proc = SpecJobProcess(
        namespace,
        job_name,
        batch_factory=existing_batch,
        finish=finish,
        cancel=cancel,
        deadline=_LOCAL_DEADLINE,
    )
    # A strong reference: the loop keeps only a weak one (#1484).
    task = asyncio.create_task(
        _await_correction(proc, spec, clone if clone.exists() else None)
    )
    _CORRECTION_WAITERS.add(task)
    task.add_done_callback(_CORRECTION_WAITERS.discard)
    return {"status": "qa_fixing", "scheduled": True, "job": job_name}
