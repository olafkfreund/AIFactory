#!/usr/bin/env python3
"""RFC-0016 Phase-2 — shared k8s Job-per-task dispatch reference library (#191).

Pure, dependency-free builder for the per-task Kubernetes Job that runs one PARR
job (plan / build / verify) on the **thin nix-base image** with per-task Nix
toolchain provisioning (RFC-0016 §4.1 / RFC-0005 Tier A). It is the single source
of truth the per-service consumers vendor (AIFactory #671, TFactory #466,
PFactory #218), mirroring how ``nix_provisioner.py`` is shared byte-identically.

This module is intentionally I/O-free: it returns a Job manifest dict and the
dispatch/reconcile contract constants. Applying the Job, watching it, and writing
the ``job-state`` row (apis/job-state.schema.json) is the caller's job — done the
same way the live ``kube_sandbox.py`` backends already apply Jobs.

Design (matches apis/concurrency-conventions.md §3 + the proven kube_sandbox shape):
- restartPolicy Never, backoffLimit 0 (no silent retries — a retry is a new attempt
  with an incremented job-state ``attempt``), ttlSecondsAfterFinished (GC),
  activeDeadlineSeconds (deadline), automountServiceAccountToken False.
- The thin nix-base image; the task's commands run via ``nix develop`` against the
  per-task flake co-mounted in the worktree (caller wraps commands; see
  ``nix_develop_wrap``).
- Warm ``/nix/store`` mounted from the per-service nix-store PVC (RFC-0016 #197) so
  Jobs don't cold-fetch the closure.
- The task worktree co-mounted at ``/work`` from the data PVC via ``subPath``.
- Env carries the short scalar identifiers + shared-state coordinates: ``JOB_ID``,
  ``CORRELATION_KEY``, ``DATABASE_URL`` (the job writes its own job-state row),
  ``ARTIFACTS_URI`` (object-store prefix).

Run ``python3 scripts/job_dispatch.py`` for the self-test.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any

# Canonical nix-base image (RFC-0016 #198). Override per env in the consumer.
DEFAULT_NIX_IMAGE = "ghcr.io/olafkfreund/tfactory-runner-nix:latest"

# Dispatch/reconcile contract (apis/concurrency-conventions.md §3).
JOB_NAME_PREFIX = "factory"  # Job named factory-<service>-<job_id_short>
_DNS_LABEL_MAX = 63  # Kubernetes object-name (DNS-1123 label) length limit
TERMINAL_STATES = ("done", "failed", "stuck")
# The control plane reconciles by polling the job-state table, so a missed
# completion event never strands a job; reporting is idempotent on (job_id, state).
RECONCILE_BY = "postgres-poll"


@dataclass(frozen=True)
class JobSpec:
    """Inputs for one per-task Job. Short scalars only — never embed the contract
    blob (that lives in the worktree / object store)."""

    service: str  # pfactory | aifactory | tfactory
    job_id: str
    commands: list[str]  # build/verify commands; wrapped in nix develop by default
    correlation_key: str | int | None = None
    image: str = DEFAULT_NIX_IMAGE
    service_account: str | None = None  # e.g. aifactory-sandbox
    data_pvc: str | None = None  # worktree co-mount source (e.g. aifactory-data)
    worktree_subpath: str | None = None  # subPath within data_pvc -> /work
    nix_store_pvc: str | None = None  # warm /nix/store (RFC-0016 #197)
    database_url_env: str = "DATABASE_URL"
    artifacts_uri: str | None = None
    # RFC-0017 §2.3 multi-node scale-out: packed-workspace object URI. When set,
    # the Job receives WORKSPACE_URI and is expected to unpack_workspace it into
    # /work at start (replacing the RWO worktree co-mount that pins a Job to one
    # node). Default None → single-node co-mount path unchanged (#190 Stage E).
    workspace_uri: str | None = None
    cpu_limit: str = "2"
    mem_limit: str = "4Gi"
    ttl_seconds: int = 300
    deadline_seconds: int = 3600
    image_pull_secret: str | None = "ghcr-pull"  # noqa: S105 — k8s secret name, not a credential
    namespace: str = "factory"
    nix_develop: bool = True  # wrap commands in `nix develop path:/work#default`
    extra_env: dict[str, str] = field(default_factory=dict)


def _short(job_id: str) -> str:
    """k8s-safe short suffix from a job_id (DNS-1123, <=20 chars)."""
    s = re.sub(r"[^a-z0-9-]", "-", job_id.lower()).strip("-")
    return (s[-20:] or "job").strip("-") or "job"


def job_name(service: str, job_id: str) -> str:
    return f"{JOB_NAME_PREFIX}-{service}-{_short(job_id)}"


def nix_develop_wrap(commands: list[str]) -> str:
    """Wrap commands to run inside the per-task Nix env. `path:` is mandatory — a
    bare flake ref triggers Nix's git fetcher and breaks on the Job-root vs
    worktree-uid mismatch (RFC-0016 §4.1 gotcha)."""
    joined = " && ".join(commands)
    return f"nix develop path:/work#default --command bash -c {_shq(joined)}"


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def build_job_manifest(spec: JobSpec) -> dict[str, Any]:
    """Return a complete k8s Job manifest dict for one PARR task. Pure."""
    name = job_name(spec.service, spec.job_id)

    inner = (
        nix_develop_wrap(spec.commands)
        if spec.nix_develop
        else " && ".join(spec.commands)
    )

    env = [
        {"name": "JOB_ID", "value": spec.job_id},
        {"name": "FACTORY_SERVICE", "value": spec.service},
    ]
    if spec.correlation_key is not None:
        env.append({"name": "CORRELATION_KEY", "value": str(spec.correlation_key)})
    if spec.artifacts_uri:
        env.append({"name": "ARTIFACTS_URI", "value": spec.artifacts_uri})
    if spec.workspace_uri:
        env.append({"name": "WORKSPACE_URI", "value": spec.workspace_uri})
    # DATABASE_URL is injected by the consumer (often from a secretKeyRef); we only
    # name it so the Job knows to write its own job-state row.
    for k, v in spec.extra_env.items():
        env.append({"name": k, "value": v})

    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    work_co_mount = bool(spec.data_pvc and spec.worktree_subpath)
    if work_co_mount:
        volumes.append(
            {"name": "work", "persistentVolumeClaim": {"claimName": spec.data_pvc}}
        )
        mounts.append(
            {"name": "work", "mountPath": "/work", "subPath": spec.worktree_subpath}
        )
    elif spec.workspace_uri:
        # RFC-0017 #190: a packed-workspace Job has NO RWO worktree co-mount (that
        # co-mount is exactly what pins a Job to the worktree's single node). The
        # consumer (cli.main.maybe_unpack_workspace) unpacks WORKSPACE_URI into
        # /work at startup, so /work must be a WRITABLE, node-local target — an
        # emptyDir. Without it /work is the image's root-owned dir and the nonroot
        # unpack dies with ``PermissionError: /work`` (caught in live #190
        # validation). emptyDir keeps the Job node-agnostic (no PVC), which is the
        # whole point of the pack/unpack handoff.
        volumes.append({"name": "work", "emptyDir": {}})
        mounts.append({"name": "work", "mountPath": "/work"})
    if spec.nix_store_pvc:
        volumes.append(
            {
                "name": "nix-store",
                "persistentVolumeClaim": {"claimName": spec.nix_store_pvc},
            }
        )
        # Warm store: persist the realised closures across Jobs (RFC-0016 #197).
        mounts.append({"name": "nix-store", "mountPath": "/nix/store"})

    container = {
        "name": "task",
        "image": spec.image,
        "command": ["bash", "-c", inner],
        "workingDir": "/work" if (work_co_mount or spec.workspace_uri) else "/",
        "env": env,
        "resources": {"limits": {"cpu": spec.cpu_limit, "memory": spec.mem_limit}},
    }
    if mounts:
        container["volumeMounts"] = mounts

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "containers": [container],
    }
    if spec.service_account:
        pod_spec["serviceAccountName"] = spec.service_account
    if volumes:
        pod_spec["volumes"] = volumes
    if spec.image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": spec.image_pull_secret}]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": spec.namespace,
            "labels": {
                "app": spec.service,
                "factory.io/job-id": _short(spec.job_id),
                "factory.io/kind": "task",
            },
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": spec.ttl_seconds,
            "activeDeadlineSeconds": spec.deadline_seconds,
            "template": {
                "metadata": {"labels": {"app": spec.service}},
                "spec": pod_spec,
            },
        },
    }


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _selftest() -> None:
    spec = JobSpec(
        service="aifactory",
        job_id="proj-abc:042-go-hello",
        commands=["go test ./..."],
        correlation_key=482,
        service_account="aifactory-sandbox",
        data_pvc="aifactory-data",
        worktree_subpath="workspaces/x/worktrees/tasks/042-go-hello",
        nix_store_pvc="aifactory-nix-store",
        artifacts_uri="s3://factory-artifacts/aifactory/482/proj-abc/",
    )
    m = build_job_manifest(spec)
    name = m["metadata"]["name"]
    _require(m["kind"] == "Job", "kind must be Job")
    _require(
        m["spec"]["backoffLimit"] == 0, "backoffLimit must be 0 (no silent retries)"
    )
    _require(name.startswith("factory-aifactory-"), f"name prefix: {name}")
    _require(
        len(name) <= _DNS_LABEL_MAX and re.fullmatch(r"[a-z0-9-]+", name) is not None,
        f"DNS-1123: {name}",
    )
    ps = m["spec"]["template"]["spec"]
    _require(ps["serviceAccountName"] == "aifactory-sandbox", "SA")
    _require(ps["automountServiceAccountToken"] is False, "no token automount")
    c = ps["containers"][0]
    _require(c["image"] == DEFAULT_NIX_IMAGE, "nix-base image")
    _require("nix develop path:/work#default" in c["command"][2], "nix develop wrap")
    _require("go test ./..." in c["command"][2], "task command present")
    mount_paths = {mt["mountPath"] for mt in c["volumeMounts"]}
    _require(mount_paths == {"/work", "/nix/store"}, f"mounts: {mount_paths}")
    names = {e["name"] for e in c["env"]}
    _require(
        {"JOB_ID", "CORRELATION_KEY", "ARTIFACTS_URI", "FACTORY_SERVICE"} <= names,
        "env",
    )
    # No-store / no-worktree (PFactory light planning) still builds.
    bare = build_job_manifest(
        JobSpec(
            service="pfactory", job_id="s1", commands=["echo hi"], nix_develop=False
        )
    )
    bare_cmd = bare["spec"]["template"]["spec"]["containers"][0]["command"][2]
    _require(bare_cmd == "echo hi", "bare command (no nix wrap)")
    _require(
        "volumes" not in bare["spec"]["template"]["spec"],
        "no volumes when none requested",
    )
    sys.stdout.write(
        "job_dispatch self-test: PASS — manifest, nix-develop wrap, "
        "warm-store + worktree mounts, env, bare fallback\n"
    )


if __name__ == "__main__":
    _selftest()
