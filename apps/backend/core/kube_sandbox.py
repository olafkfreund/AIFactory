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

from core.factory_sandbox import RunResult  # reuse the shared result shape

logger = logging.getLogger(__name__)


def build_job_manifest(name: str, image: str, commands: list[str], *,
                       namespace: str = "factory",
                       image_pull_secret: str = "ghcr-pull",
                       cpus: str = "2", memory: str = "2Gi",
                       ttl_seconds: int = 120, timeout: int = 600) -> dict:
    """Pure builder for the per-task Job manifest. No cluster access."""
    command = " && ".join(commands)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app": "aifactory-sandbox"}},
        "spec": {
            "ttlSecondsAfterFinished": ttl_seconds,   # auto-GC after completion
            "backoffLimit": 0,                          # no retries — one shot
            "activeDeadlineSeconds": timeout,
            "template": {
                "metadata": {"labels": {"app": "aifactory-sandbox", "job-name": name}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,  # the gate needs no k8s API
                    "imagePullSecrets": [{"name": image_pull_secret}],
                    "containers": [{
                        "name": "gate",
                        "image": image,
                        "command": ["bash", "-c", command],
                        "resources": {"limits": {"cpu": cpus, "memory": memory}},
                    }],
                },
            },
        },
    }


class KubeJobSandbox:
    def __init__(self, image: str, *, namespace: str = "factory", **manifest_kw):
        self.image = image
        self.namespace = namespace
        self.manifest_kw = manifest_kw

    async def _run_async(self, commands: list[str], timeout: int) -> RunResult:
        from kubernetes_asyncio import client, config
        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 - dev/test fallback
            await config.load_kube_config()

        name = "fsbx-" + uuid.uuid4().hex[:10]
        manifest = build_job_manifest(name, self.image, commands,
                                      namespace=self.namespace, timeout=timeout,
                                      **self.manifest_kw)
        api = client.ApiClient()
        batch, core = client.BatchV1Api(api), client.CoreV1Api(api)
        try:
            await batch.create_namespaced_job(self.namespace, manifest)
            succeeded = False
            for _ in range(max(1, timeout // 3)):
                st = (await batch.read_namespaced_job_status(name, self.namespace)).status
                if st and st.succeeded:
                    succeeded = True
                    break
                if st and st.failed:
                    break
                await asyncio.sleep(3)
            pods = await core.list_namespaced_pod(self.namespace,
                                                  label_selector=f"job-name={name}")
            output = ""
            if pods.items:
                try:
                    output = await core.read_namespaced_pod_log(
                        pods.items[0].metadata.name, self.namespace)
                except Exception as exc:  # noqa: BLE001
                    output = f"(log unavailable: {exc})"
            return RunResult(succeeded, 0 if succeeded else 1, (output or "").strip(), [])
        finally:
            try:
                await batch.delete_namespaced_job(name, self.namespace,
                                                  propagation_policy="Background")
            except Exception:  # noqa: BLE001 - ttlSecondsAfterFinished GCs anyway
                pass
            await api.close()

    def run(self, commands: list[str], *, workdir: str | None = None,
            timeout: int = 600) -> RunResult:
        # workdir mount deferred (PVC RWO can't co-mount; needs RWX or tar-inject).
        return asyncio.run(self._run_async(commands, timeout))


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
