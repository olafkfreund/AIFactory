"""Kubernetes Job-per-task sandbox backend (RFC-0005 #68).

The AIFactory pod has no container runtime, so the in-cluster verification
sandbox launches each task's commands as an ephemeral **Kubernetes Job** from the
per-task image (validated live: the `aifactory-sandbox` SA can create/delete jobs
and read pod logs). Same `(level, commands) -> (ok, output)` contract as the
docker-run `factory_sandbox.container_backend`, so it drops into `gate_runner`.

`build_job_manifest()` is pure (no cluster / no client) and unit-tested; the async
lifecycle (`create -> watch -> logs -> delete`) uses `kubernetes_asyncio` and is
validated in-cluster, not mocked.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from core.factory_sandbox import RunResult  # reuse the shared result shape

logger = logging.getLogger(__name__)


def build_job_manifest(
    name: str,
    image: str,
    commands: list[str],
    *,
    namespace: str = "factory",
    image_pull_secret: str = "ghcr-pull",
    cpus: str = "2",
    memory: str = "2Gi",
    ttl_seconds: int = 120,
    timeout: int = 600,
    repo_pvc: str | None = None,
    repo_subpath: str | None = None,
    workdir: str = "/work",
    repo_ro: bool = False,
    nix_store_pvc: str | None = None,
) -> dict:
    """Pure builder for the per-task Job manifest. No cluster access.

    When ``repo_pvc`` is given, the task worktree is co-mounted into the gate
    container at ``workdir`` via the named PVC + ``repo_subpath`` (PVC-relative),
    and that becomes the container's working directory — so code-reading gates
    (lint/test/build) see the real worktree files, not just the toolchain. The
    co-mount relies on the gate Job being scheduled on the same node as the
    AIFactory pod that already holds the RWO PVC (true on a single-node /
    local-path cluster); it is omitted entirely when ``repo_pvc`` is None,
    leaving the toolchain-only behavior unchanged.

    When ``nix_store_pvc`` is given (RFC-0016 #197), the whole ``/nix`` tree is
    served from that warm-store PVC so per-task Nix Jobs stop cold-fetching the
    toolchain closure every run. ``/nix`` (not just ``/nix/store``) is mounted
    because the store and its sqlite db (``/nix/var/nix/db``) must stay
    consistent — a store mounted without its db looks empty to nix and defeats
    the cache. An initContainer seeds the PVC from the image's own ``/nix`` on
    first use (when empty), so the nix binary's own closure survives the overlay;
    subsequent Jobs find a populated store and skip the fetch. Omitted entirely
    when ``nix_store_pvc`` is None, leaving cold-fetch behavior unchanged.
    """
    command = " && ".join(commands)
    container: dict[str, Any] = {
        "name": "gate",
        "image": image,
        "command": ["bash", "-c", command],
        "resources": {"limits": {"cpu": cpus, "memory": memory}},
    }
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,  # the gate needs no k8s API
        "imagePullSecrets": [{"name": image_pull_secret}],
        "containers": [container],
    }
    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    if repo_pvc:
        container["workingDir"] = workdir
        mounts.append(
            {
                "name": "repo",
                "mountPath": workdir,
                "subPath": repo_subpath,
                "readOnly": repo_ro,
            }
        )
        volumes.append(
            {
                "name": "repo",
                "persistentVolumeClaim": {"claimName": repo_pvc, "readOnly": repo_ro},
            }
        )
    if nix_store_pvc:
        mounts.append({"name": "nix-store", "mountPath": "/nix"})
        volumes.append(
            {
                "name": "nix-store",
                "persistentVolumeClaim": {"claimName": nix_store_pvc},
            }
        )
        # Seed the warm store from the image's baked-in /nix on first use, else
        # the empty PVC overlay would hide nix's own closure (the nix binary
        # itself lives in /nix/store) and the Job could not run.
        pod_spec["initContainers"] = [
            {
                "name": "seed-nix-store",
                "image": image,
                "command": [
                    "sh",
                    "-c",
                    "if [ ! -e /warm/store ]; then "
                    "cp -a /nix/. /warm/ && echo 'seeded warm nix store'; "
                    "else echo 'warm nix store already populated'; fi",
                ],
                "volumeMounts": [{"name": "nix-store", "mountPath": "/warm"}],
            }
        ]
    if mounts:
        container["volumeMounts"] = mounts
    if volumes:
        pod_spec["volumes"] = volumes
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "aifactory-sandbox"},
        },
        "spec": {
            "ttlSecondsAfterFinished": ttl_seconds,  # auto-GC after completion
            "backoffLimit": 0,  # no retries — one shot
            "activeDeadlineSeconds": timeout,
            "template": {
                "metadata": {"labels": {"app": "aifactory-sandbox", "job-name": name}},
                "spec": pod_spec,
            },
        },
    }


def _pvc_subpath(workdir: str | None, data_root: str) -> str | None:
    """PVC-relative subPath for a gate's absolute ``workdir``, or None.

    The worktree lives under the data PVC mounted at ``data_root``
    (e.g. ``/home/nonroot/.aifactory/workspaces/<proj>/.aifactory/worktrees/...``).
    Stripping that prefix yields the PVC-relative path to mount via subPath.
    Returns None when ``workdir`` is unset or outside the PVC — the caller then
    runs a toolchain-only Job (no repo mount), which is honest rather than wrong.
    """
    if not workdir:
        return None
    root = data_root.rstrip("/") + "/"
    norm = workdir.rstrip("/")
    if norm == data_root.rstrip("/"):
        return ""  # the PVC root itself
    if not norm.startswith(root):
        return None
    return norm[len(root) :]


def _exit_code_from_pod(pod: object, *, job_succeeded: bool) -> tuple[bool, int]:
    """(succeeded, exit_code) from a Job pod's terminated container state.

    RFC-0005: a k8s Job only exposes a boolean succeeded/failed, which collapses
    every non-zero exit to "failed" and loses the real status — so a gate would
    report a synthetic 0/1 instead of the command's true exit code. Read the
    first container's terminated ``exit_code`` when available; fall back to the
    Job flag (synthetic 0/1) when the pod/state is missing. Pure + testable.
    """
    try:
        statuses = pod.status.container_statuses or []  # type: ignore[attr-defined]
        terminated = (
            statuses[0].state.terminated if statuses and statuses[0].state else None
        )
        if terminated is not None and terminated.exit_code is not None:
            code = int(terminated.exit_code)
            return code == 0, code
    except Exception:  # noqa: BLE001 — best-effort; keep the Job-flag fallback
        pass
    return job_succeeded, (0 if job_succeeded else 1)


class KubeJobSandbox:
    def __init__(
        self,
        image: str,
        *,
        namespace: str = "factory",
        repo_pvc: str | None = None,
        data_root: str = "/home/nonroot/.aifactory",
        **manifest_kw,
    ):
        self.image = image
        self.namespace = namespace
        self.repo_pvc = repo_pvc
        self.data_root = data_root
        self.manifest_kw = manifest_kw

    async def _run_async(
        self, commands: list[str], timeout: int, workdir: str | None = None
    ) -> RunResult:
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 - dev/test fallback
            await config.load_kube_config()

        name = "fsbx-" + uuid.uuid4().hex[:10]
        repo_kw: dict = {}
        if self.repo_pvc:
            subpath = _pvc_subpath(workdir, self.data_root)
            if subpath is not None:
                repo_kw = {"repo_pvc": self.repo_pvc, "repo_subpath": subpath}
            else:
                logger.info(
                    "[kube-sandbox] workdir %r outside data root %r; "
                    "running toolchain-only Job (no repo mount)",
                    workdir,
                    self.data_root,
                )
        manifest = build_job_manifest(
            name,
            self.image,
            commands,
            namespace=self.namespace,
            timeout=timeout,
            **repo_kw,
            **self.manifest_kw,
        )
        api = client.ApiClient()
        batch, core = client.BatchV1Api(api), client.CoreV1Api(api)
        try:
            await batch.create_namespaced_job(self.namespace, manifest)
            succeeded = False
            for _ in range(max(1, timeout // 3)):
                # read the Job object (needs only `get jobs`), not the jobs/status
                # subresource — keeps the sandbox Role least-privilege.
                st = (await batch.read_namespaced_job(name, self.namespace)).status
                if st and st.succeeded:
                    succeeded = True
                    break
                if st and st.failed:
                    break
                await asyncio.sleep(3)
            pods = await core.list_namespaced_pod(
                self.namespace, label_selector=f"job-name={name}"
            )
            output = ""
            succeeded, exit_code = succeeded, (0 if succeeded else 1)
            if pods.items:
                pod = pods.items[0]
                try:
                    output = await core.read_namespaced_pod_log(
                        pod.metadata.name, self.namespace
                    )
                except Exception as exc:  # noqa: BLE001
                    output = f"(log unavailable: {exc})"
                # Prefer the container's REAL exit code over the Job's
                # succeeded/failed flag (RFC-0005): the flag collapses every
                # non-zero to "failed" and loses the actual status.
                succeeded, exit_code = _exit_code_from_pod(pod, job_succeeded=succeeded)
            return RunResult(succeeded, exit_code, (output or "").strip(), [])
        finally:
            try:
                await batch.delete_namespaced_job(
                    name, self.namespace, propagation_policy="Background"
                )
            except Exception:  # noqa: BLE001 - ttlSecondsAfterFinished GCs anyway
                pass
            await api.close()

    def run(
        self, commands: list[str], *, workdir: str | None = None, timeout: int = 600
    ) -> RunResult:
        # When repo_pvc is set, workdir (the gate's worktree cwd) is co-mounted
        # via the data PVC's subPath; otherwise the Job is toolchain-only.
        return asyncio.run(self._run_async(commands, timeout, workdir))


def kube_job_backend(image: str, **kw):
    """#74-runner backend backed by an ephemeral k8s Job per call."""
    sb = KubeJobSandbox(image, **kw)

    def run(level: str, commands: list[str]):
        try:
            res = sb.run(commands)
        except Exception as exc:  # noqa: BLE001 - sandbox issues are gate failures
            return 1, f"kube-sandbox error: {exc}"
        code = res.exit_code if res.ok else 1
        return code, res.output

    return run
