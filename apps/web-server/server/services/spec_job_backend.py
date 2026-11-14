"""Kubernetes Job dispatch and Process adapter for spec creation (#1932)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from factory_common.logsafe import sanitize_log

from . import build_backend
from .build_log_stream import KubeJobLogStreamer, LineSource

_log = logging.getLogger(__name__)
_ENV_SPEC_BACKEND = "AIFACTORY_SPEC_BACKEND"
_LIVE_SPEC_JOBS: set[str] = set()


def spec_kubejob_enabled() -> bool:
    value = os.environ.get(_ENV_SPEC_BACKEND, "").strip().lower()
    if value in {"", "subprocess"}:
        return False
    if value == "kubejob":
        return True
    raise build_backend.BuildBackendUnavailableError(
        f"Invalid {_ENV_SPEC_BACKEND}; valid values: subprocess, kubejob"
    )


def live_spec_job_names() -> set[str]:
    return _LIVE_SPEC_JOBS.copy()


def build_spec_job_manifest(  # noqa: PLR0913 - manifest coordinates
    *,
    task_id: str,
    fetch_id: str,
    spec_id: str,
    workspace_uri: str,
    extra_env: dict[str, str],
    model: str | None = None,
    complexity: str | None = None,
) -> dict[str, Any]:
    from core.job_dispatch import (  # noqa: PLC0415 - backend path is set at startup
        DEFAULT_NIX_IMAGE,
        JobSpec,
        build_job_manifest,
    )

    argv = [
        "python",
        str(
            Path(build_backend._resolve_run_py_path()).parent / "runners/spec_runner.py"
        ),
        "--task-file",
        "/work/.aifactory/spec_task.md",
        "--project-dir",
        "/work",
        "--spec-dir",
        f"/work/.aifactory/specs/{spec_id}",
        "--no-build",
        "--auto-approve",
    ]
    if model:
        argv.extend(("--model", model))
    if complexity:
        argv.extend(("--complexity", complexity))
    service_account = (
        os.environ.get(
            build_backend._ENV_SERVICE_ACCOUNT, build_backend._DEFAULT_SERVICE_ACCOUNT
        ).strip()
        or build_backend._DEFAULT_SERVICE_ACCOUNT
    )
    spec = JobSpec(
        service="aifactory",
        job_id=f"{task_id}-spec-{fetch_id[:8]}",
        commands=[shlex.join(argv)],
        image=build_backend._resolve_build_image(DEFAULT_NIX_IMAGE),
        service_account=service_account,
        namespace=build_backend._dispatch_namespace(),
        data_pvc=None,
        worktree_subpath=None,
        deadline_seconds=7200,
        nix_develop=False,
        extra_env=extra_env,
        workspace_uri=workspace_uri,
        automount_service_account_token=False,
    )
    # #1932: this agent reads untrusted issue text, so it gets the Copilot
    # (GitHub) login only when the spec model actually runs on Copilot.
    cred_files = tuple(
        (key, rel)
        for key, rel in build_backend._CLI_CRED_FILES
        if "copilot" not in key or (model or "").lower().startswith("copilot:")
    )
    return build_backend._inject_build_path(
        build_backend._inject_seed_creds(build_job_manifest(spec), cred_files)
    )


def _prepare_spec_workspace(
    project_path: Path, spec_id: str, spec_dir: Path, task_text: str, job_id: str
) -> str:
    from core.artifact_store import (  # noqa: PLC0415 - avoid boto3 at import time
        ArtifactRef,
        ArtifactStore,
        pack_workspace,
    )
    from core.workspace import copy_spec_to_worktree  # noqa: PLC0415

    clone = project_path / ".aifactory/worktrees/specs" / spec_id
    try:
        if clone.exists():
            shutil.rmtree(clone)
        clone.parent.mkdir(parents=True, exist_ok=True)
        build_backend._git(
            ["clone", "--local", "--no-hardlinks", str(project_path), str(clone)]
        )
        if spec_dir.is_symlink() or any(
            path.is_symlink() for path in spec_dir.rglob("*")
        ):
            raise build_backend.BuildBackendUnavailableError(
                "Spec directory contains a symlink"
            )
        # A tracked symlink here would send the copy or the task text outside
        # the clone, onto control-plane files.
        if any(
            (clone / rel).is_symlink()
            for rel in (
                ".aifactory",
                ".aifactory/specs",
                f".aifactory/specs/{spec_id}",
                ".aifactory/spec_task.md",
            )
        ):
            raise build_backend.BuildBackendUnavailableError(
                "Cloned spec path is a symlink"
            )
        copy_spec_to_worktree(spec_dir, clone, spec_id)
        (clone / ".aifactory/spec_task.md").write_text(task_text)
        return cast(
            str,
            pack_workspace(
                ArtifactStore(),
                ArtifactRef(service="aifactory", job_id=job_id, role="workspace"),
                clone,
            ),
        )
    finally:
        if clone.exists():
            shutil.rmtree(clone)


def _finish_spec_fetch(spec_dir: Path, fetch_id: str, auto_approve: bool) -> int:
    from core.workspace_fetch import fetch_spec_files  # noqa: PLC0415
    from review.state import ReviewState  # noqa: PLC0415

    state = ReviewState.load(spec_dir)
    if "spec.md" not in fetch_spec_files(spec_dir, fetch_id):
        # A partial fetch may have replaced the plan: drop any earlier approval.
        state.invalidate(spec_dir)
        return 1
    if auto_approve:
        state.approve(spec_dir, approved_by="auto")
    elif not state.is_approval_valid(spec_dir):
        state.invalidate(spec_dir)
    return 0


class SpecJobProcess:
    """Process-compatible view of a running spec Job."""

    pid: None = None

    def __init__(  # noqa: PLR0913 - Process adapter coordinates
        self,
        namespace: str,
        job_name: str,
        *,
        batch_factory: Callable[
            [], Awaitable[Any]
        ] = build_backend.KubeJobBuildBackend._batch_api,
        finish: Callable[[], Awaitable[int]],
        poll_interval: float = 10.0,
        line_source: LineSource | None = None,
    ) -> None:
        self.namespace = namespace
        self.job_name = job_name
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self._stop = asyncio.Event()
        self._runner = asyncio.create_task(
            self._run(batch_factory, finish, poll_interval, line_source)
        )

    async def _run(
        self,
        batch_factory: Callable[[], Awaitable[Any]],
        finish: Callable[[], Awaitable[int]],
        poll_interval: float,
        line_source: LineSource | None,
    ) -> None:
        outcome: str | None = None

        async def feed(line: str) -> None:
            self.stdout.feed_data((line + "\n").encode())

        async def active() -> bool:
            return outcome is None and not self._stop.is_set()

        stream = asyncio.create_task(
            KubeJobLogStreamer(log_sink=feed, line_source=line_source).stream(
                namespace=self.namespace,
                job_name=self.job_name,
                spec_id=self.job_name,
                job_active=active,
            )
        )
        batch: Any = None
        try:
            batch = await batch_factory()
            while True:
                if self._stop.is_set():
                    try:
                        await batch.delete_namespaced_job(
                            self.job_name,
                            self.namespace,
                            propagation_policy="Background",
                        )
                    except Exception:  # noqa: BLE001 - stop still completes
                        _log.exception(
                            "[spec_job_backend] could not delete Job %s/%s",
                            sanitize_log(self.namespace),
                            sanitize_log(self.job_name),
                        )
                    self.returncode = -15
                    break
                outcome = await build_backend.KubeJobBuildBackend._job_outcome(
                    batch, self.namespace, self.job_name
                )
                if outcome in {"succeeded", "failed", "gone"}:
                    break
                outcome = None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=poll_interval)
            if self.returncode is None:
                if outcome == "succeeded":
                    try:
                        self.returncode = await finish()
                    except Exception:  # noqa: BLE001 - failed fetch is failed Job
                        _log.exception(
                            "[spec_job_backend] fetch failed for Job %s/%s",
                            sanitize_log(self.namespace),
                            sanitize_log(self.job_name),
                        )
                        self.returncode = 1
                else:
                    self.returncode = 1
        except Exception:  # noqa: BLE001 - keep the Process surface terminal
            _log.exception(
                "[spec_job_backend] polling failed for Job %s/%s",
                sanitize_log(self.namespace),
                sanitize_log(self.job_name),
            )
            self.returncode = 1
        finally:
            _LIVE_SPEC_JOBS.discard(self.job_name)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stream, timeout=30)
            if not stream.done():
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream
            self.stdout.feed_eof()
            api = getattr(batch, "api_client", None)
            if api is not None:
                with contextlib.suppress(Exception):
                    await api.close()

    async def wait(self) -> int:
        await asyncio.shield(self._runner)
        if self.returncode is None:
            raise RuntimeError("spec Job runner ended without a return code")
        return self.returncode

    def send_signal(self, sig: int) -> None:
        del sig
        self._stop.set()

    def terminate(self) -> None:
        self._stop.set()

    def kill(self) -> None:
        self._stop.set()


class KubeJobSpecBackend:
    async def dispatch(  # noqa: PLR0913 - dispatch coordinates are keyword-only
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        spec_dir: Path,
        task_text: str,
        model: str | None,
        complexity: str | None,
        auto_approve: bool,
        oauth_token: str | None,
        mcp_env: dict[str, str],
        marker_nonce: str,
        batch: Any = None,
    ) -> SpecJobProcess:
        from core.workspace_fetch import SPEC_FETCH_ID_ENV  # noqa: PLC0415

        fetch_id = uuid4().hex
        uri = await asyncio.to_thread(
            _prepare_spec_workspace,
            project_path,
            spec_id,
            spec_dir,
            task_text,
            f"{task_id}-spec-{fetch_id[:8]}",
        )
        env = build_backend.build_job_env(oauth_token)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        env.update(mcp_env)
        env.update(
            {
                "AIFACTORY_MARKER_NONCE": marker_nonce,
                "AIFACTORY_TASK_ID": task_id,
                SPEC_FETCH_ID_ENV: fetch_id,
            }
        )
        if complexity == "simple":
            env["QUICK_MODE"] = "true"
        manifest = build_spec_job_manifest(
            task_id=task_id,
            fetch_id=fetch_id,
            spec_id=spec_id,
            workspace_uri=uri,
            extra_env=env,
            model=model,
            complexity=complexity,
        )
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
            "[spec_job_backend] dispatched Job %s/%s",
            sanitize_log(namespace),
            sanitize_log(job_name),
        )

        async def finish() -> int:
            return await asyncio.to_thread(
                _finish_spec_fetch, spec_dir, fetch_id, auto_approve
            )

        async def existing_batch() -> object:
            return batch

        return SpecJobProcess(
            namespace, job_name, batch_factory=existing_batch, finish=finish
        )
