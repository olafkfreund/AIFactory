"""RFC-0016 #671 — control/execution split: run.py as a k8s Job.

The full coder loop (``run.py``) runs today as an in-pod asyncio subprocess of
the web-server (``AgentService._spawn_task_execution``). This module adds the
**opt-in** Job-dispatch path so the build can run as a per-task Kubernetes Job
instead — the web-server becomes a thin control plane, builds scale across nodes
and survive a control-plane roll.

Default OFF. The backend is selected by ``AIFACTORY_BUILD_BACKEND``:

  * ``subprocess`` (default) — today's in-pod behaviour, unchanged.
  * ``kubejob``               — dispatch a k8s Job that runs ``run.py``.

Design (apis/concurrency-conventions.md §3 + the proven ``kube_sandbox`` shape):

* The Job manifest comes from the shared, byte-identical ``job_dispatch`` builder
  (vendored at ``core/job_dispatch.py``): the **AIFactory runtime image**
  (``_resolve_build_image`` — NOT the thin nix gate substrate, see #671 below),
  ``aifactory-sandbox`` SA, warm ``/nix/store`` PVC, the task worktree co-mounted
  at ``/work``, ``restartPolicy=Never``/``backoffLimit=0`` (a retry is a new
  attempt), TTL GC, and ``activeDeadlineSeconds``.
* The Job runs ``run.py`` for the spec (NOT wrapped in ``nix develop`` here — the
  contract's per-task env wrapping is run.py's own concern; the dispatch wraps the
  *entrypoint*, not the build commands). Because the entrypoint is a plain
  ``bash -c "python run.py …"``, the Job image MUST ship bash + python + run.py +
  the agent SDK — i.e. the aifactory image, resolved from ``AIFACTORY_BUILD_IMAGE``
  / the downward-API ``AIFACTORY_IMAGE`` (the #671 fix: the thin nix gate image
  has no bash/python on PATH outside ``nix develop`` → StartError, no logs).
  ``AIFACTORY_SANDBOX_IMAGE`` stays the thin nix image for gates and is untouched.
* The Job writes its own job-state row (``running`` → terminal) + artifacts; the
  control plane **reconciles by polling Postgres** (``reconcile_by_poll``), so a
  missed completion event never strands a build.
* Idempotent terminal reporting is keyed on (job_id, terminal-state) in the store
  (``mark_terminal`` is a harmless no-op on a second write).
* The reaper (``reap_vanished_jobs``) marks a ``running`` k8s-job row failed with a
  reason when its Job disappears / exceeds its deadline without a terminal write.

This module keeps cluster I/O (apply/watch/delete) thin and isolated so it is
unit-testable with a mocked k8s client — no real cluster is needed for tests.
The pure manifest builder + backend selection + reconcile + reaper are all
exercised against fakes.

RFC-0017 #680: the dispatched Job's logs are now streamed Job-native into the
cockpit log stream + the rmux Live Agent Console (``build_log_stream`` +
``AgentService._start_kubejob_log_stream``), matching the in-pod path's two log
sinks. That removes the only remaining reason kubejob isn't the default. The
default flip (``AIFACTORY_BUILD_BACKEND=kubejob``, RFC-0016 #671) is now READY
pending a live validation run (a real build green Job-native with the console
intact) — deliberately NOT flipped in this change.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Backend selection (RFC-0016 #671). Default OFF — today's in-pod subprocess.
BACKEND_SUBPROCESS = "subprocess"
BACKEND_KUBEJOB = "kubejob"
_DEFAULT_BACKEND = BACKEND_SUBPROCESS

# Env coordinates reused from the shipped gate substrate (gate_runner.py) so the
# build Job lands on the same SA / data PVC / warm store / namespace.
_ENV_BACKEND = "AIFACTORY_BUILD_BACKEND"
# Build-Job image (#671 defect fix). The build Job runs run.py via a plain
# ``bash -c "python run.py …"`` entrypoint (nix_develop=False), so it needs an
# image that actually ships bash + python + run.py + the agent SDK — i.e. the
# AIFactory runtime image itself, NOT the thin ``tfactory-runner-nix`` gate
# substrate (which has no bash/python on PATH outside a ``nix develop`` shell;
# the build Job pod hits StartError on it and run.py never runs).
#
# Resolution precedence (first non-empty wins):
#   1. AIFACTORY_BUILD_IMAGE  — explicit operator override for the build Job ONLY.
#   2. AIFACTORY_IMAGE        — the running Deployment's own image ref, injected by
#                               the chart (the pod can't read its own image from
#                               the downward API, so the chart sets this to the
#                               resolved aifactory image). This makes the build
#                               Job default to the exact image the control plane
#                               runs — guaranteed python-capable.
#   3. DEFAULT_NIX_IMAGE      — last-resort fallback (logged WARNING): keeps the
#                               manifest buildable in dev/test where neither var
#                               is set. A real cluster always sets one of the
#                               above; this image cannot run the plain entrypoint.
#
# AIFACTORY_SANDBOX_IMAGE is deliberately NOT consulted here: it is shared with
# the gate substrate (gate_runner.py) and must stay the thin nix image so gates
# keep working. Repointing it would break gates; the build needs its own image.
_ENV_BUILD_IMAGE = "AIFACTORY_BUILD_IMAGE"
_ENV_RUNNING_IMAGE = "AIFACTORY_IMAGE"
_ENV_REPO_PVC = "AIFACTORY_SANDBOX_REPO_PVC"
_ENV_NIX_STORE_PVC = "AIFACTORY_NIX_STORE_PVC"
_ENV_DATA_ROOT = "AIFACTORY_DATA_ROOT"
_ENV_NAMESPACE = "AIFACTORY_SANDBOX_NAMESPACE"
_ENV_SERVICE_ACCOUNT = "AIFACTORY_BUILD_SA"
_ENV_DEADLINE = "AIFACTORY_BUILD_DEADLINE_SECONDS"

# Defaults mirror gate_runner.py + the kube_sandbox SA.
_DEFAULT_REPO_PVC = "aifactory-data"
_DEFAULT_DATA_ROOT = "/home/nonroot/.aifactory"
_DEFAULT_NAMESPACE = "factory"
_DEFAULT_SERVICE_ACCOUNT = "aifactory-sandbox"
_DEFAULT_DEADLINE_SECONDS = 6 * 3600  # a full build is long-lived

# The worktree a build runs in (mirrors agent_service._spawn_task_execution).
_WORKTREE_TEMPLATE = ".aifactory/worktrees/tasks/{spec_id}"

# Terminal lifecycle states the Job may write (apis/job-state.schema.json). A
# build that reaches one of these is done from the control plane's view.
_TERMINAL_STATES = ("done", "failed", "stuck", "review")


def selected_backend() -> str:
    """Return the configured build backend, normalised + validated.

    Unknown values fall back to ``subprocess`` (fail safe — never silently
    route builds to an unimplemented backend) and log a warning.
    """
    raw = (os.environ.get(_ENV_BACKEND, _DEFAULT_BACKEND) or "").strip().lower()
    if raw in (BACKEND_SUBPROCESS, BACKEND_KUBEJOB):
        return raw
    _log.warning(
        "[build_backend] unknown %s=%r — falling back to %r",
        _ENV_BACKEND, raw, BACKEND_SUBPROCESS,
    )
    return BACKEND_SUBPROCESS


def kubejob_enabled() -> bool:
    """True when builds should run as a k8s Job (RFC-0016 #671). Default False."""
    return selected_backend() == BACKEND_KUBEJOB


def _worktree_subpath(data_root: str, project_path: Path, spec_id: str) -> str | None:
    """PVC-relative subPath of the build worktree, or None if outside the PVC.

    The worktree lives under the data PVC mounted at ``data_root``. Stripping
    that prefix yields the path to co-mount via ``subPath``. Returns None when
    the project is outside the data root (dev/test on a laptop) — the Job then
    runs without the worktree mount, which is honest rather than wrong.
    """
    worktree = project_path / _WORKTREE_TEMPLATE.format(spec_id=spec_id)
    root = data_root.rstrip("/") + "/"
    norm = str(worktree).rstrip("/")
    if not norm.startswith(root):
        return None
    return norm[len(root):]


def _resolve_build_image(default_nix_image: str) -> str:
    """Resolve the image for the run.py BUILD Job (#671 defect fix).

    See ``_ENV_BUILD_IMAGE`` for the precedence rationale. The build Job runs a
    plain ``bash -c "python run.py …"`` entrypoint, so it must land on a
    python-capable image (the aifactory runtime), never the thin nix gate image.

    * ``AIFACTORY_BUILD_IMAGE``  — explicit build-only override.
    * ``AIFACTORY_IMAGE``        — the running Deployment's own image (chart-set).
    * ``default_nix_image``      — last-resort fallback; logs a WARNING because
      the thin nix image cannot execute the plain entrypoint outside ``nix
      develop``. Reached only in dev/test where no image env is configured.
    """
    build_image = os.environ.get(_ENV_BUILD_IMAGE, "").strip()
    if build_image:
        return build_image
    running_image = os.environ.get(_ENV_RUNNING_IMAGE, "").strip()
    if running_image:
        return running_image
    _log.warning(
        "[build_backend] neither %s nor %s set — falling back to %r for the "
        "build Job. This thin nix image has no bash/python outside `nix "
        "develop` and CANNOT run the run.py entrypoint; set %s (or the "
        "downward-API %s) to the aifactory runtime image.",
        _ENV_BUILD_IMAGE, _ENV_RUNNING_IMAGE, default_nix_image,
        _ENV_BUILD_IMAGE, _ENV_RUNNING_IMAGE,
    )
    return default_nix_image


def build_run_py_job_manifest(
    *,
    task_id: str,
    project_path: Path,
    spec_id: str,
    correlation_key: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the k8s Job manifest that runs ``run.py`` for one build (#671).

    Pure (no cluster access) so it is unit-testable. Delegates the manifest
    shape to the shared ``job_dispatch`` builder (SA, warm store, worktree
    co-mount, no-retry, TTL, deadline) and supplies the AIFactory-specific
    entrypoint:

        python run.py --spec <spec_id> --project-dir /work --auto-continue --force

    The image is the AIFactory runtime image (``_resolve_build_image``), NOT the
    thin nix gate substrate: the entrypoint is a plain ``bash -c "python run.py
    …"`` (``nix_develop=False``), so it needs an image with bash + python +
    run.py + the agent SDK. run.py drives its own per-task env materialization;
    toolchains come from Nix at build time via run.py's own paths, not from the
    Job substrate. (Using the thin nix image here was the #671 defect: the pod
    hit StartError because bash/python aren't on PATH outside ``nix develop``.)
    """
    # Deferred import: the vendored builder lives on the backend path (core.*),
    # added to sys.path at startup. Resolve it lazily so pure web-server import
    # paths (and tests that don't need it) stay clean.
    from core.job_dispatch import DEFAULT_NIX_IMAGE, JobSpec, build_job_manifest

    image = _resolve_build_image(DEFAULT_NIX_IMAGE)
    repo_pvc = os.environ.get(_ENV_REPO_PVC, _DEFAULT_REPO_PVC).strip() or None
    nix_store_pvc = os.environ.get(_ENV_NIX_STORE_PVC, "").strip() or None
    data_root = os.environ.get(_ENV_DATA_ROOT, _DEFAULT_DATA_ROOT)
    namespace = os.environ.get(_ENV_NAMESPACE, _DEFAULT_NAMESPACE).strip() or _DEFAULT_NAMESPACE
    service_account = (
        os.environ.get(_ENV_SERVICE_ACCOUNT, _DEFAULT_SERVICE_ACCOUNT).strip()
        or _DEFAULT_SERVICE_ACCOUNT
    )
    try:
        deadline = int(os.environ.get(_ENV_DEADLINE, _DEFAULT_DEADLINE_SECONDS))
    except (TypeError, ValueError):
        deadline = _DEFAULT_DEADLINE_SECONDS

    worktree_subpath = (
        _worktree_subpath(data_root, project_path, spec_id) if repo_pvc else None
    )

    # The build entrypoint: run.py against the co-mounted worktree at /work.
    # Mirrors agent_service._spawn_task_execution's headless invocation. NOT
    # nix-develop-wrapped (nix_develop=False) — run.py drives its own per-task
    # env; the entrypoint just needs an interpreter, which the aifactory build
    # image (resolved above) provides.
    commands = [
        "python run.py "
        f"--spec {spec_id} --project-dir /work --auto-continue --force"
    ]

    spec = JobSpec(
        service="aifactory",
        job_id=task_id,
        commands=commands,
        correlation_key=correlation_key,
        image=image,
        service_account=service_account,
        data_pvc=repo_pvc if worktree_subpath is not None else None,
        worktree_subpath=worktree_subpath,
        nix_store_pvc=nix_store_pvc,
        namespace=namespace,
        deadline_seconds=deadline,
        nix_develop=False,
        extra_env=extra_env or {},
    )
    return build_job_manifest(spec)


class KubeJobBuildBackend:
    """Dispatch + reconcile + reap the run.py Job (RFC-0016 #671).

    Cluster I/O is confined to the ``_apply`` / ``_job_exists`` / ``_delete``
    helpers, each taking an injectable ``batch`` client so tests pass a fake.
    The orchestration (build manifest → apply → record worker_ref →
    reconcile-by-poll → reap) is pure-ish and unit-tested.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    # -- cluster I/O (thin, injectable for tests) ---------------------------

    @staticmethod
    async def _batch_api() -> Any:
        """Build a kubernetes_asyncio BatchV1Api, in-cluster or via kubeconfig.

        Mirrors ``kube_sandbox.KubeJobSandbox._run_async`` config loading.
        """
        from kubernetes_asyncio import client, config

        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 - dev/test fallback
            await config.load_kube_config()
        return client.BatchV1Api(client.ApiClient())

    # -- dispatch -----------------------------------------------------------

    async def dispatch(
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        correlation_key: str | None = None,
        batch: Any = None,
    ) -> str:
        """Create the run.py Job and record its worker_ref. Returns the Job name.

        The caller has already reserved the durable slot (the job-state row is
        ``running`` with ``worker_ref={kind:subprocess}`` from ``admit``); we
        overwrite worker_ref with the k8s-job reference so the reconcile/reaper
        loops can find the Job. The Job itself flips the row to its terminal
        state when it finishes; we never block on it here.
        """
        manifest = build_run_py_job_manifest(
            task_id=task_id,
            project_path=project_path,
            spec_id=spec_id,
            correlation_key=correlation_key,
        )
        namespace = manifest["metadata"]["namespace"]
        job_name = manifest["metadata"]["name"]

        owns_client = batch is None
        if owns_client:
            batch = await self._batch_api()
        try:
            await batch.create_namespaced_job(namespace, manifest)
        finally:
            if owns_client:
                api = getattr(batch, "api_client", None)
                if api is not None:
                    await api.close()

        await self._store.set_worker_ref(
            task_id,
            {"kind": "k8s-job", "namespace": namespace, "job_name": job_name},
        )
        _log.info(
            "[build_backend] dispatched run.py Job %s/%s for task %s",
            namespace, job_name, task_id,
        )
        return job_name

    async def delete_job(self, job_id: str, batch: Any = None) -> bool:
        """Delete a running build's k8s Job (RFC-0016 #671 stop path).

        Looks up the Job ref from the durable row and deletes it with Background
        propagation (so the pod is reaped too). Returns True when a delete was
        issued. The caller frees the durable slot separately (mark_terminal).
        """
        state = await self._store.get_state(job_id)
        if state is None:
            return False
        ref = state.get("worker_ref") or {}
        if ref.get("kind") != "k8s-job":
            return False
        job_name = ref.get("job_name")
        namespace = ref.get("namespace")
        if not job_name or not namespace:
            return False

        owns_client = batch is None
        if owns_client:
            batch = await self._batch_api()
        try:
            await batch.delete_namespaced_job(
                job_name, namespace, propagation_policy="Background"
            )
            _log.info(
                "[build_backend] deleted k8s Job %s/%s for task %s",
                namespace, job_name, job_id,
            )
            return True
        finally:
            if owns_client:
                api = getattr(batch, "api_client", None)
                if api is not None:
                    await api.close()

    # -- reconcile-by-poll --------------------------------------------------

    async def reconcile_by_poll(self, job_id: str) -> str | None:
        """Reconcile one job from its durable state (apis §3 reconcile-by-poll).

        Returns the terminal ``lifecycle_state`` (``done``/``failed``/``stuck``)
        the Job wrote, or ``None`` when it is still ``running`` (or the row is
        gone). The control plane polls this rather than relying on a k8s watch
        event, so a missed event never strands a build. Pure read — terminal
        reporting is the Job's job; this only observes it.
        """
        state = await self._store.get_state(job_id)
        if state is None:
            return None
        lifecycle = state.get("lifecycle_state")
        if lifecycle in _TERMINAL_STATES:
            return lifecycle
        return None

    # -- reaper -------------------------------------------------------------

    async def reap_vanished_jobs(
        self,
        *,
        now: float | None = None,
        deadline_seconds: int | None = None,
        batch: Any = None,
    ) -> list[str]:
        """Fail ``running`` k8s-job rows whose Job vanished / blew the deadline.

        Closes the "lanes pending, no verdict" stall class (TFactory #464) at
        the orchestration layer: for each ``running`` row whose worker is a k8s
        Job, if the Job object no longer exists AND no terminal state was
        written, mark the row ``failed`` with a reason. Idempotent — a row that
        the Job already transitioned out of ``running`` is skipped.

        Returns the job_ids it reaped (for observability / tests).
        ``now``/``deadline_seconds`` let tests drive the deadline branch without
        wall-clock sleeps; in production a vanished Job (TTL-GC'd or evicted) is
        the dominant signal.
        """
        import time as _time

        reaped: list[str] = []
        rows = await self._store.get_active_kubejobs()
        if not rows:
            return reaped

        owns_client = batch is None
        if owns_client:
            batch = await self._batch_api()
        try:
            for row in rows:
                job_id = row["job_id"]
                job_name = row.get("job_name")
                namespace = row.get("namespace")
                if not job_name or not namespace:
                    # No usable ref — can't verify; leave for the deadline path.
                    continue

                exists = await self._job_exists(batch, namespace, job_name)
                if exists:
                    # Optional deadline guard for a wedged-but-present Job.
                    if deadline_seconds is not None:
                        updated = row.get("updated_at")
                        ts = _to_epoch(updated)
                        clock = now if now is not None else _time.time()
                        if ts is not None and (clock - ts) > deadline_seconds:
                            await self._fail(
                                job_id,
                                f"k8s Job {namespace}/{job_name} exceeded the "
                                f"control-plane deadline ({deadline_seconds}s) "
                                "without a terminal write",
                            )
                            reaped.append(job_id)
                    continue

                # Job is gone but the row is still running → it vanished without
                # writing a terminal state. Don't strand it.
                await self._fail(
                    job_id,
                    f"k8s Job {namespace}/{job_name} disappeared without a "
                    "terminal write (evicted / GC'd / crashed before report)",
                )
                reaped.append(job_id)
        finally:
            if owns_client:
                api = getattr(batch, "api_client", None)
                if api is not None:
                    await api.close()
        return reaped

    async def _fail(self, job_id: str, reason: str) -> None:
        """Mark a stranded build failed (idempotent via the store)."""
        try:
            await self._store.mark_terminal(job_id, "failed", error=reason)
            _log.warning("[build_backend] reaped stranded build %s: %s", job_id, reason)
        except Exception:  # noqa: BLE001 - reaper must never crash the loop
            _log.exception("[build_backend] could not reap %s", job_id)

    @staticmethod
    async def _job_exists(batch: Any, namespace: str, job_name: str) -> bool:
        """True when the Job object still exists in the cluster.

        A 404 (ApiException status 404) means it's gone; any other error is
        treated as "exists" (fail safe — don't reap a build we can't verify).
        """
        try:
            await batch.read_namespaced_job(job_name, namespace)
            return True
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            if status == 404:
                return False
            _log.warning(
                "[build_backend] could not verify Job %s/%s (%s) — assuming present",
                namespace, job_name, exc,
            )
            return True


def _to_epoch(value: Any) -> float | None:
    """Best-effort epoch seconds from a datetime / number, else None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:  # noqa: BLE001
            return None
    return None
