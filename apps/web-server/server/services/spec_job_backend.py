"""Kubernetes Job dispatch and Process adapter for spec creation (#1932)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import threading
import time
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
# The Job's activeDeadlineSeconds (7200) plus grace: past it, stop polling.
_LOCAL_DEADLINE_SECONDS = 7500.0
_API_CALL_TIMEOUT = 30.0
_LOG_LINE_MAX = 16000  # chars: under the 64 KiB reader limit at 4 bytes each
_LOG_BUFFER_MAX = 1 << 20


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
    service_account = build_backend._job_service_account()
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
    # #1932/#2025: only the spec model's login (shared rule), plus Claude: the
    # spec pipeline always summarizes phases with sonnet (orchestrator.py).
    cred_files = build_backend.cred_files_for_models([model, "sonnet"])
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


def _finish_spec_fetch(
    spec_dir: Path,
    fetch_id: str,
    auto_approve: bool,
    cancel: threading.Event | None = None,
) -> int:
    from core.workspace_fetch import fetch_spec_files  # noqa: PLC0415
    from review.state import ReviewState  # noqa: PLC0415

    state = ReviewState.load(spec_dir)
    failed: list[str] = []
    fetched = fetch_spec_files(spec_dir, fetch_id, failed, cancel)
    plan_file = spec_dir / "implementation_plan.json"
    # A plan that exists but could not be read is not "no plan pushed".
    if (cancel is not None and cancel.is_set()) or "implementation_plan.json" in failed:
        state.invalidate(spec_dir)
        return 1
    # No spec.md, or a new spec.md beside a plan this Job did not return: a
    # partial fetch must not pair fresh and stale files under one approval.
    if "spec.md" not in fetched or (
        plan_file.exists() and "implementation_plan.json" not in fetched
    ):
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
        cancel: threading.Event | None = None,
        deadline: float = _LOCAL_DEADLINE_SECONDS,
    ) -> None:
        self.namespace = namespace
        self.job_name = job_name
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.truncated = False  # a log line was cut or dropped: stdout is incomplete
        self._stop = asyncio.Event()
        self._cancel = cancel or threading.Event()
        self._deadline = deadline
        self._runner = asyncio.create_task(
            self._run(batch_factory, finish, poll_interval, line_source)
        )

    async def _feed(self, line: str) -> None:
        # shortcut: reads StreamReader._buffer; no public unread size. Drop
        # lines while a consumer lags rather than grow without bound.
        if len(self.stdout._buffer) > _LOG_BUFFER_MAX:  # type: ignore[attr-defined]
            self.truncated = True
            return
        if len(line) > _LOG_LINE_MAX:
            self.truncated = True
        self.stdout.feed_data((line[:_LOG_LINE_MAX] + "\n").encode())

    async def _delete_job(self, batch: Any) -> None:
        try:
            await asyncio.wait_for(
                batch.delete_namespaced_job(
                    self.job_name, self.namespace, propagation_policy="Background"
                ),
                timeout=_API_CALL_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - stop still completes
            _log.exception(
                "[spec_job_backend] could not delete Job %s/%s",
                sanitize_log(self.namespace),
                sanitize_log(self.job_name),
            )

    async def _poll_outcome(self, batch: Any) -> str:
        try:
            return await asyncio.wait_for(
                build_backend.KubeJobBuildBackend._job_outcome(
                    batch, self.namespace, self.job_name
                ),
                timeout=_API_CALL_TIMEOUT,
            )
        except TimeoutError:  # a stalled call must not skip the deadline
            return "running"

    async def _run(
        self,
        batch_factory: Callable[[], Awaitable[Any]],
        finish: Callable[[], Awaitable[int]],
        poll_interval: float,
        line_source: LineSource | None,
    ) -> None:
        outcome: str | None = None

        async def active() -> bool:
            return outcome is None and not self._stop.is_set()

        stream = asyncio.create_task(
            KubeJobLogStreamer(log_sink=self._feed, line_source=line_source).stream(
                namespace=self.namespace,
                job_name=self.job_name,
                spec_id=self.job_name,
                job_active=active,
            )
        )
        batch: Any = None
        expires = time.monotonic() + self._deadline
        try:
            batch = await batch_factory()
            while True:
                timed_out = time.monotonic() > expires
                if timed_out:
                    _log.error(
                        "[spec_job_backend] Job %s/%s polling deadline passed",
                        sanitize_log(self.namespace),
                        sanitize_log(self.job_name),
                    )
                if self._stop.is_set() or timed_out:
                    await self._delete_job(batch)
                    self.returncode = 1 if timed_out else -15
                    outcome = "failed"  # ends the log follow
                    break
                outcome = await self._poll_outcome(batch)
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
                # A stopped Job's logs are not drained: stop_task awaits this.
                await asyncio.wait_for(
                    stream, timeout=0.1 if self._stop.is_set() else 30
                )
            if not stream.done():
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream
            self.stdout.feed_eof()
            api = getattr(batch, "api_client", None)
            if api is not None:
                with contextlib.suppress(Exception):
                    await api.close()
            # Last step: a stop during the fetch, the drain or the close must
            # not leave a 0, which would start a build.
            self.returncode = -15 if self._stop.is_set() else self.returncode

    async def wait(self) -> int:
        await asyncio.shield(self._runner)
        if self.returncode is None:
            raise RuntimeError("spec Job runner ended without a return code")
        return self.returncode

    def send_signal(self, sig: int) -> None:
        del sig
        self._stop.set()
        self._cancel.set()

    def terminate(self) -> None:
        self.send_signal(15)

    def kill(self) -> None:
        self.send_signal(9)


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
        # #2023: after every merge into env, so none can restore the bucket-wide key.
        from .job_s3_scope import scope_job_s3  # noqa: PLC0415

        await asyncio.to_thread(
            scope_job_s3,
            env,
            [(None, f"{task_id}-spec-{fetch_id[:8]}"), (None, fetch_id)],
        )
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

        cancel = threading.Event()

        async def finish() -> int:
            return await asyncio.to_thread(
                _finish_spec_fetch, spec_dir, fetch_id, auto_approve, cancel
            )

        async def existing_batch() -> object:
            return batch

        return SpecJobProcess(
            namespace,
            job_name,
            batch_factory=existing_batch,
            finish=finish,
            cancel=cancel,
        )
