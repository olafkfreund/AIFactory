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


# The unpack step's program. Kept out of the command list: adjacent string
# literals in a list read as a missing comma (CodeQL flagged exactly that), and
# a one-line program is easier to check here than inside a manifest.
# APP_BACKEND_PATH comes from the image — a hardcoded path was wrong, the code
# lives under /home/projects/….
_UNPACK_PROGRAM = (
    "import os,sys;"
    "sys.path.insert(0,os.environ['APP_BACKEND_PATH']);"
    "from core.artifact_store import ArtifactStore,unpack_workspace;"
    "unpack_workspace(ArtifactStore(),sys.argv[1],sys.argv[2])"
)


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
    workspace_uri: str | None = None,
    unpack_image: str | None = None,
    store_env: dict[str, str] | None = None,
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

    When ``workspace_uri`` is given (and no ``repo_pvc``), the code arrives by
    the PACKED path instead: an initContainer running ``unpack_image`` — the
    AIFactory image, which carries the code and the object-store credentials —
    unpacks the archive into an emptyDir that the gate container then works in.
    This is how a gate reaches the code on the multi-node/packed path, where the
    worktree lives in the build Job's own emptyDir and no PVC subPath can reach
    it (AIFactory#1491/#1524). The gate image itself needs no store credentials
    and no AIFactory code.

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
    # #812 (Factory#274 compensating controls), corrected by #840.
    # No readOnlyRootFilesystem — nix writes /nix/var.
    #
    # Capabilities are dropped, then the minimum added back for the root nix user,
    # each one established by a real gate Job on the cluster, not by argument:
    #   DAC_OVERRIDE — write the uid-65532 worktree the control plane co-mounts
    #   FOWNER       — chmod/utimes on those 65532-owned files (git/tar)
    #   CHOWN        — ownership preservation on `cp -a`
    #   SETUID/SETGID/KILL — the image ships `build-users-group = nixbld` (+32
    #     nixbld users), so any LOCAL build makes nix setuid to a build user and
    #     reap it. Without these, `nix develop` dies "setting uid: Operation not
    #     permitted / cannot kill processes for uid '30001'" the moment a
    #     derivation cannot be substituted from the binary cache. This matters
    #     MORE since #830/#253 dropped the warm store: a cold /nix substitutes
    #     most paths but still builds the shell env locally (observed:
    #     python3-*-env.drv, nix-shell-env.drv).
    container_hardening: dict[str, Any] = {
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "capabilities": {
            "drop": ["ALL"],
            "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"],
        },
    }
    container: dict[str, Any] = {
        "name": "gate",
        "image": image,
        "command": ["bash", "-c", command],
        "resources": {"limits": {"cpu": cpus, "memory": memory}},
        "securityContext": dict(container_hardening),
    }
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,  # the gate needs no k8s API
        # Pin the default seccomp profile (previously unset = Unconfined on most
        # CRI defaults).
        #
        # runAsNonRoot is deliberately NOT set (#840). #812 set it here on the
        # premise that "task images declare a non-root USER" — true of the BUILD
        # image (aifactory:*-nix is USER nonroot, and job_dispatch rightly keeps
        # runAsNonRoot), false of the GATE image: AIFACTORY_SANDBOX_IMAGE is
        # factory-runner-nix, which is USER 0:0 because nix builds run as root
        # and nix must write /nix/var. The kubelet then refused the container
        # outright — "container has runAsNonRoot and image will run as root",
        # CreateContainerConfigError — so every gate Job died before running a
        # single command. TFactory#651 reached this conclusion first and declined
        # to set it for exactly this reason; this restores parity. Do NOT "fix"
        # a future recurrence with runAsUser: the image's /nix is root-owned.
        "securityContext": {
            "seccompProfile": {"type": "RuntimeDefault"},
        },
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
    elif workspace_uri:
        # No `unpack_image or image` fallback: the gate image is DEFINED as the
        # one without AIFactory code or store credentials, so defaulting to it
        # would turn a misconfiguration into a confusing gate failure attributed
        # to the code under test. Refuse instead.
        if not unpack_image:
            raise ValueError(
                "workspace_uri needs unpack_image: the unpack initContainer must "
                "run an image carrying the AIFactory code and object-store "
                "credentials (the build image), never the gate image"
            )
        # The gate image has neither the AIFactory code nor store credentials,
        # so the unpack runs in an initContainer on the build image and lands in
        # a shared emptyDir. `data` filtering / traversal vetting lives in
        # artifact_store.unpack_workspace, which is what runs here.
        container["workingDir"] = workdir
        mounts.append({"name": "repo", "mountPath": workdir})
        volumes.append({"name": "repo", "emptyDir": {}})
        pod_spec["initContainers"] = [
            {
                "name": "unpack-workspace",
                "image": unpack_image,
                "command": ["python3", "-c", _UNPACK_PROGRAM, workspace_uri, workdir],
                "env": [
                    {"name": k, "value": v}
                    for k, v in sorted((store_env or {}).items())
                ],
                "securityContext": dict(container_hardening),
                "volumeMounts": [{"name": "repo", "mountPath": workdir}],
            }
        ]
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
        #
        # Two concurrent gate Jobs can both start this initContainer before
        # either has populated /warm/store, or one can start while the other
        # is mid-copy -- a plain check-then-copy lets either see a partial
        # store (#1545). Copy into a scratch dir on the SAME filesystem first,
        # then `mv` each top-level entry into place; a `mv` within one
        # filesystem is an atomic rename, so a concurrent reader either sees
        # an entry or does not, never a half-written one. `store` moves last
        # so it keeps meaning "the seed is complete" for the `[ -e /warm/store ]`
        # check. `mv -n` never overwrites, so a Job that loses the race just
        # discards its own copy instead of corrupting the winner's.
        _seed_script = (
            "if [ -e /warm/store ]; then "
            "echo 'warm nix store already populated'; "
            "else "
            'tmp=/warm/.seed-$$; rm -rf "$tmp" && mkdir -p "$tmp" '
            '&& cp -a /nix/. "$tmp/" '
            '&& for e in "$tmp"/*; do '
            'n=$(basename "$e"); [ "$n" = store ] && continue; '
            'mv -n "$e" /warm/ 2>/dev/null || rm -rf "$e"; '
            "done "
            '&& if [ -d "$tmp/store" ]; then '
            'mv -n "$tmp/store" /warm/store 2>/dev/null || rm -rf "$tmp/store"; '
            "fi; "
            'rm -rf "$tmp"; '
            "echo 'seeded warm nix store (or lost the race to a concurrent seed)'; "
            "fi"
        )
        pod_spec.setdefault("initContainers", []).append(
            {
                "name": "seed-nix-store",
                "image": image,
                "command": ["sh", "-c", _seed_script],
                # Same image as the gate → same non-root uid; just pin the
                # escalation/capability hardening (#812).
                "securityContext": dict(container_hardening),
                "volumeMounts": [{"name": "nix-store", "mountPath": "/warm"}],
            }
        )
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
                # factory.io/kind=task puts gate pods under the chart's
                # per-task NetworkPolicy (#812) — the selectorLabels policy
                # never matches Job pods.
                "metadata": {
                    "labels": {
                        "app": "aifactory-sandbox",
                        "job-name": name,
                        "factory.io/kind": "task",
                    }
                },
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


def repo_is_mountable(workdir: str | None, data_root: str) -> bool:
    """True when a nested Job can co-mount the repo at *workdir* from the PVC.

    False means the code lives somewhere no other pod can reach — the packed
    path's ``/work`` emptyDir being the live case (AIFactory#1491): the build Job
    unpacks the worktree into a pod-local dir, so a Job that mounts the data PVC
    sees no repo at all and the gate measures an empty directory.
    """
    return _pvc_subpath(workdir, data_root) is not None


def _job_failure_reason(status: object) -> str:
    """The k8s reason a Job failed (e.g. ``DeadlineExceeded``), or "".

    A Job that hits ``activeDeadlineSeconds`` is killed with no terminated
    container state, so the exit code falls back to a synthetic 1 — the same
    value a genuinely failing test produces. The reason is the only thing that
    tells those apart.
    """
    for cond in getattr(status, "conditions", None) or []:
        if getattr(cond, "type", "") == "Failed":
            return str(getattr(cond, "reason", "") or "Failed")
    return ""


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

    @staticmethod
    async def _poll_job(
        batch: Any, name: str, namespace: str, budget_seconds: int
    ) -> tuple[bool, str]:
        """Poll the Job until it succeeds/fails, or the budget runs out.

        Returns (succeeded, failure_reason). A Job can transition to
        Failed/DeadlineExceeded during the loop's LAST `asyncio.sleep`, after
        the last in-loop read already found it still running — the `for/else`
        below (entered only when the loop exhausted its iterations without a
        `break`) is one more read taken specifically to still catch that,
        otherwise the `[job ...]` reason silently disappears (#1545).
        """
        succeeded = False
        failure_reason = ""
        for _ in range(max(1, budget_seconds // 3)):
            # read the Job object (needs only `get jobs`), not the jobs/status
            # subresource — keeps the sandbox Role least-privilege.
            st = (await batch.read_namespaced_job(name, namespace)).status
            if st and st.succeeded:
                succeeded = True
                break
            if st and st.failed:
                failure_reason = _job_failure_reason(st)
                break
            await asyncio.sleep(3)
        else:
            st = (await batch.read_namespaced_job(name, namespace)).status
            if st and st.succeeded:
                succeeded = True
            elif st and st.failed:
                failure_reason = _job_failure_reason(st)
        return succeeded, failure_reason

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
            succeeded, failure_reason = await self._poll_job(
                batch, name, self.namespace, timeout
            )
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
            text = (output or "").strip()
            if failure_reason:
                # A Job killed by its deadline never finished the command, so its
                # output is a truncated transcript of whatever it got through —
                # indistinguishable from a command that ran and failed. Name the
                # reason, or "the toolchain download did not finish" reads as
                # "your tests failed" (AIFactory#1491 family).
                text = f"[job {failure_reason}] {text}"
            return RunResult(succeeded, exit_code, text, [])
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
