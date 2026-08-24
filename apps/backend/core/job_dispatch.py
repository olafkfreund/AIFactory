#!/usr/bin/env python3
"""RFC-0016 Phase-2 — shared k8s Job-per-task dispatch reference library (#191).

Pure, dependency-free builder for the per-task Kubernetes Job that runs one PARR
job (plan / build / verify) on the **thin nix-base image** with per-task Nix
toolchain provisioning (RFC-0016 §4.1 / RFC-0005 Tier A). It is the single source
of truth the per-service consumers vendor, mirroring how ``nix_provisioner.py``
is shared byte-identically.

Vendored, byte-exact (Factory#477, Factory#483):

    AIFactory  apps/backend/core/job_dispatch.py
    TFactory   apps/backend/tools/runners/job_dispatch.py

and kept identical by the hub's drift gate — ``scripts/check_verification_core_drift.py``
run from each consumer's ``verification-core-drift`` workflow. PFactory (#218) holds
no copy and needs none: it runs a bounded pooled-worker model, not Job-per-task, so
it builds no Job manifest at all (the citation in its ``plan/service.py`` names this
file as the seam to use IF a governed planning Job ever lands). If it later vendors
this file, add it to the gate's ``SERVICE_LAYOUTS`` in the SAME change — an
unregistered copy is exactly the silent divergence Factory#477 opened on.

TWO WAYS TO CONSUME THIS FILE (Factory#483)
-------------------------------------------
``build_job_manifest`` builds the whole manifest and is the right entry point when
a service has nothing to add. TFactory does have something to add — credential
seeding via an initContainer, a 23-var provider-env allowlist, a service account it
genuinely needs a token for — so it builds its own manifest and calls the *policy*
helpers below instead:

    ``job_labels`` / ``task_pod_labels``   the label rules, constructed not copied
    ``trace_env``                          the trace-context + OTLP env (Factory#607)
    ``assert_job_policy``                  the rules, checked, in the consumer's tests

That split exists because the alternative was tried and failed. TFactory restated
the naming, labelling and hardening rules by hand under a comment citing this file,
which is a fork with a citation: nothing compares a constant to its source, and no
drift gate can help when there is no vendored file to compare. The cost was not
theoretical — the pod-label defect below arrived THREE times independently and was
filed as four unconnected symptoms (TFactory#885, AIFactory#1107, Factory#456,
Factory#458).

WHAT IS SHARED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
``assert_job_policy`` checks only rules that must hold for EVERY factory Job.
Several neighbouring rules are legitimately per-service and are deliberately left
out of it, because asserting them would force a consumer to break itself to pass:

- pod ``securityContext``: TFactory's nix lanes run as ROOT on purpose (the
  nixos/nix image builds as root; forcing nonroot breaks every lane, #840), so
  ``runAsNonRoot``/``runAsUser`` cannot be universal.
- ``automountServiceAccountToken``: False here, but TFactory's verify-orchestration
  Job dispatches nested per-lane Jobs through the k8s API and needs a token.
- container capability add-backs: TFactory adds SETUID/SETGID/KILL back because
  local nix builds setuid to a nixbld user (#623).
- the Job NAME prefix: ``kube_sandbox`` uses ``tfsbx-`` and portal-UI uses
  ``portal-ui-``. Only DNS-1123 VALIDITY is universal, not the prefix.

Anything on that list is a per-service decision with a per-service test. Everything
``assert_job_policy`` does check is a rule a service is not free to opt out of.

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
- The task worktree co-mounted at ``/work`` from the data PVC via ``subPath`` —
  OR, for multi-node scale-out (RFC-0017 §2.3), fetched from object storage: set
  ``workspace_uri`` and the Job receives ``WORKSPACE_URI`` to ``unpack_workspace``
  into ``/work`` at start and ``pack_workspace`` its outputs back at the end
  (scripts/artifact_store.py). That replaces the RWO worktree co-mount, which pins
  the Job to one node; the RWO mount stays available for the single-node path and
  is removed only by the gitops rollout (out of scope here).
- Env carries the short scalar identifiers + shared-state coordinates: ``JOB_ID``,
  ``CORRELATION_KEY``, ``DATABASE_URL`` (the job writes its own job-state row),
  ``ARTIFACTS_URI`` (object-store prefix), ``WORKSPACE_URI`` (packed-workspace URI),
  and — Factory#607 — the W3C ``TRACEPARENT`` plus the OTLP export config the Job
  needs to CONTINUE the dispatcher's trace rather than start a new one. See
  ``trace_env`` for why that is not a "nice to have".

Run ``python3 scripts/job_dispatch.py`` for the self-test.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# Canonical nix-base image (RFC-0016 #198). Override per env in the consumer.
DEFAULT_NIX_IMAGE = "ghcr.io/olafkfreund/tfactory-runner-nix:latest"

# Dispatch/reconcile contract (apis/concurrency-conventions.md §3).
JOB_NAME_PREFIX = "factory"  # Job named factory-<service>-<job_id_short>
_DNS_LABEL_MAX = 63  # Kubernetes object-name (DNS-1123 label) length limit
# The numeric uid behind the build image's `USER nonroot` (Wolfi/apko standard).
# Verified from the image itself, not assumed:
#   $ docker run --rm --entrypoint sh <build-image> -c id
#   uid=65532(nonroot) gid=65532(nonroot) groups=65532(nonroot)
# It is also the uid the control plane creates worktree files as, so pinning it
# preserves ownership rather than changing it (#848).
NONROOT_UID = 65532
# Every service that dispatches Jobs through this contract. This is what makes the
# pod-label rule checkable as a RULE rather than against one literal (Factory#479):
# no task pod's `app` label may equal any of these, because that is exactly what
# each service's Service selects.
SERVICES = ("aifactory", "cfactory", "pfactory", "tfactory")
# The Secret holding the collector's OTLP auth header, kept fresh by the
# ``observe-otlp-auth-sync`` CronJob (factory-gitops). Named here rather than
# read from env because it is a cluster fact with one value, and the Job needs a
# REFERENCE to it rather than this pod's copy — see trace_env().
OTLP_AUTH_SECRET = "otel-otlp-auth"  # noqa: S105 — k8s Secret name, not a credential
OTLP_AUTH_SECRET_KEY = "headers"  # noqa: S105 — the Secret's KEY name, not a credential
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


# Twin of ``nix_provisioner._strip_npx``, deliberately duplicated: these two
# files are INDEPENDENT vendoring units and each fork places them in a different
# package (``core/`` vs ``tools/runners/``), so a cross-import would not resolve
# after vendoring. Keep the two in step — see nix_provisioner for the full
# reasoning and the live evidence.
#
# `npx <tool>` re-fetches <tool> from the npm registry even when the flake has
# already put a version-matched copy on PATH, then runs THAT copy, silently
# undoing the provisioning. Only tools the flake provably ships are rewritten;
# stripping `npx` from one it does not ship would turn a working fetch into
# "command not found".
_NPX_PROVIDED_BY_FLAKE = ("playwright",)

# argv is ["npx", "<tool>", ...]; anything shorter cannot be a rewrite target.
_NPX_MIN_PARTS = 2


def _strip_npx(command: str) -> str:
    """Drop a leading ``npx`` when the flake supplies the tool itself."""
    parts = command.split()
    if len(parts) >= _NPX_MIN_PARTS and parts[0] == "npx" and parts[1] in _NPX_PROVIDED_BY_FLAKE:
        return " ".join(parts[1:])
    return command


def nix_develop_wrap(commands: list[str]) -> str:
    """Wrap commands to run inside the per-task Nix env. `path:` is mandatory — a
    bare flake ref triggers Nix's git fetcher and breaks on the Job-root vs
    worktree-uid mismatch (RFC-0016 §4.1 gotcha)."""
    joined = " && ".join(_strip_npx(c) for c in commands)
    return f"nix develop path:/work#default --command bash -c {_shq(joined)}"


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def job_labels(service: str, job_id: str) -> dict[str, str]:
    """Labels for the Job OBJECT.

    ``app`` deliberately IS the service name here, unlike the pod labels below: a
    Job object is never a Service endpoint and never a ``kubectl exec`` target, so
    ``kubectl get jobs -l app=aifactory`` stays useful. Only pods route traffic.
    """
    return {
        "app": service,
        "factory.io/job-id": _short(job_id),
        "factory.io/kind": "task",
    }


def task_pod_labels(service: str, *, role: str = "task") -> dict[str, str]:
    """Labels for the POD TEMPLATE of a dispatched Job. Use this, never a literal.

    ``app`` is DELIBERATELY NOT ``service``. The ``aifactory`` Service selects
    exactly {"app": "aifactory"} and a Service selector is a SUBSET match, so a task
    pod carrying that label joins the Service as an endpoint. The pod listens on
    nothing, so kube-proxy hands it a share of real API traffic and answers with
    connection refused — a fraction of requests, for as long as a build runs, which
    reads as flakiness rather than as a fault. It also makes ``kubectl exec
    deploy/aifactory`` land in a build pod, which is how it was finally noticed
    (mid-demo). Confirmed three times over: TFactory's portal-ui browser test took
    the portal it was testing offline and then reported it broken (TFactory#885);
    AIFactory's task Jobs did it to the aifactory API (AIFactory#1107); and
    factory-gitops' minio-create-bucket Job did it to the minio Service.

    ``role`` distinguishes pods of different dispatchers within one service —
    TFactory's nix lanes are ``tfactory-sandbox`` (its Job NetworkPolicy selects
    that value, so it is load-bearing) and its portal-UI Jobs are
    ``tfactory-portal-ui``. It is NOT the same axis as ``factory.io/kind``, which
    stays literally "task" for every dispatcher: that is the "this pod is a
    dispatched task, not a serving pod" label the per-task NetworkPolicy selects
    (#812), and splitting it per role would silently drop pods out of that policy.

    ``factory.io/service`` carries the association ``app`` used to, so anything that
    legitimately wants "task pods of aifactory" can still ask for it — this removes
    an accidental selector match, not the information.

    Because ``role`` may not be empty, an ``app`` label equal to the service name is
    not reachable through this function. That is the entire point of it existing.
    """
    _require(bool(role), f"role must not be empty (app would collide with {service!r})")
    return {
        "app": f"{service}-{role}",
        "factory.io/service": service,
        "factory.io/kind": "task",
    }


def current_traceparent() -> str | None:
    """The W3C ``traceparent`` of the caller's ACTIVE span, or None.

    Lazily imports OpenTelemetry so this module stays importable (and this file
    stays vendorable) in a process that has no OTel installed at all — which is
    every unit-test run and every CLI invocation. ``inject`` writes nothing into
    the carrier when there is no valid active span, so "not tracing right now"
    and "OTel absent" both come back as None and both mean the same thing here:
    there is no trace for the Job to continue.
    """
    try:
        from opentelemetry.propagate import inject  # noqa: PLC0415

        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier.get("traceparent")
    except Exception:  # noqa: BLE001 — tracing must never break dispatch
        return None


def trace_env(service: str) -> list[dict[str, Any]]:
    """Env entries that let a dispatched Job continue the dispatcher's trace.

    Factory#607. Before this, a Job got ``CORRELATION_KEY`` and nothing else, so
    a PARR trace covered the control plane and stopped exactly where the work
    starts. ``CORRELATION_KEY`` is a LOG-correlation key, not trace context: it
    cannot make a Job's spans children of the span that dispatched them, and the
    Job had no OTLP config either, so it could not have exported one if it had.

    Returns, when the dispatcher itself is exporting:

    ``TRACEPARENT``                    the active span, so the Job's spans are
                                       children of it and share its trace_id.
                                       Omitted when there is no active span —
                                       there is then no trace to join, and an
                                       unparented Job span is span volume with
                                       no question attached.
    ``OTEL_EXPORTER_OTLP_ENDPOINT``    passed through from this pod's own env.
    ``OTEL_EXPORTER_OTLP_PROTOCOL``    likewise; ``http/protobuf`` is the fleet's.
    ``OTEL_EXPORTER_OTLP_HEADERS``     a ``secretKeyRef``, NEVER the value.
    ``OTEL_SERVICE_NAME``              ``<service>-job``, so the collector can
                                       tell a Job's spans from its dispatcher's.

    Two deliberate properties:

    1. **The credential is a reference, not a copy.** The collector header is a
       Basic credential. Copying this pod's value into a Job manifest would (a)
       write it into an object every namespace reader can ``kubectl get -o yaml``
       and (b) freeze it: ``observe-otlp-auth-sync`` rotates that Secret hourly
       (Factory#606), so a manifest built from a value captured at pod start goes
       stale without anything saying so. A ``secretKeyRef`` is resolved by the
       kubelet at pod start, which is the same thing the long-lived Deployments
       do and for the same reason.
    2. **Endpoint unset means nothing is added.** Outside the cluster and in
       every test run there is no collector, and a Job that carries OTLP config
       pointed at nothing spends its exporter's retry budget for no spans.

    Ambient by design: it reads this process's OTEL_* env and its active span
    rather than taking them as parameters. A parameter would have to be supplied
    by every caller, and a rule every caller has to remember is the shape of the
    defect this file exists to prevent (see the module docstring).
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return []
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").strip()
    env: list[dict[str, Any]] = [
        {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": endpoint},
        {"name": "OTEL_EXPORTER_OTLP_PROTOCOL", "value": protocol},
        {
            "name": "OTEL_EXPORTER_OTLP_HEADERS",
            "valueFrom": {
                "secretKeyRef": {
                    "name": OTLP_AUTH_SECRET,
                    "key": OTLP_AUTH_SECRET_KEY,
                    # The credential is optional so a cluster without the Secret
                    # dispatches Jobs that simply do not export, instead of Jobs
                    # stuck in CreateContainerConfigError. A Job that cannot
                    # trace must still be able to build.
                    "optional": True,
                }
            },
        },
        {"name": "OTEL_SERVICE_NAME", "value": f"{service}-job"},
    ]
    traceparent = current_traceparent()
    if traceparent:
        env.append({"name": "TRACEPARENT", "value": traceparent})
    return env


def assert_job_policy(manifest: dict[str, Any]) -> None:
    """Raise AssertionError unless *manifest* obeys the rules EVERY factory Job
    must obey — whoever built it, and whatever else it carries.

    This is the half of the contract a consumer that builds its own manifest still
    has to honour. Call it from that consumer's tests: the module docstring lists
    what is deliberately NOT checked here and why.
    """
    _require(manifest.get("kind") == "Job", f"kind must be Job: {manifest.get('kind')!r}")

    name = manifest["metadata"]["name"]
    _require(
        len(name) <= _DNS_LABEL_MAX
        and re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name) is not None,
        f"Job name must be a DNS-1123 label (<={_DNS_LABEL_MAX} chars, lowercase "
        f"alphanumeric and '-', not starting or ending with '-'): {name!r}. A name "
        f"built by interpolating an unsanitised id is how this breaks — run the id "
        f"through _short() first.",
    )

    _require(
        manifest["spec"]["backoffLimit"] == 0,
        "backoffLimit must be 0 — a retry is a new attempt with an incremented "
        "job-state `attempt`, never a silent one (concurrency-conventions.md 3)",
    )

    template = manifest["spec"]["template"]
    pod_labels = template["metadata"]["labels"]
    service = pod_labels.get("factory.io/service")
    _require(
        service is not None,
        f"pod label factory.io/service is required: it names which service this task "
        f"belongs to, and without it the `app` rule below cannot be checked at all. "
        f"Build pod labels with task_pod_labels(). Got: {pod_labels}",
    )
    _require(
        pod_labels.get("factory.io/kind") == "task",
        f"pod label factory.io/kind=task is required — it is what the per-task "
        f"NetworkPolicy selects (#812), and a pod without it matches no policy and "
        f"runs unrestricted. Got: {pod_labels}",
    )
    app = pod_labels.get("app")
    _require(
        app != service and app not in SERVICES,
        f"pod `app` label must not equal a service name (it would join that "
        f"service's Service and answer real traffic with connection refused — "
        f"TFactory#885 / AIFactory#1107 / Factory#456): {app!r}",
    )

    _require(
        template["spec"]["restartPolicy"] == "Never",
        "restartPolicy must be Never (retries are dispatched, not restarted)",
    )


def build_job_manifest(spec: JobSpec) -> dict[str, Any]:
    """Return a complete k8s Job manifest dict for one PARR task. Pure."""
    name = job_name(spec.service, spec.job_id)

    inner = nix_develop_wrap(spec.commands) if spec.nix_develop else " && ".join(spec.commands)

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
    # Factory#607: trace context + OTLP export config, so the Job's spans join the
    # dispatcher's trace instead of the trace ending at this boundary. Empty when
    # this process is not exporting, which keeps every off-cluster path unchanged.
    env.extend(trace_env(spec.service))
    # DATABASE_URL is injected by the consumer (often from a secretKeyRef); we only
    # name it so the Job knows to write its own job-state row.
    # extra_env goes LAST so a consumer can still override anything above it —
    # k8s takes the last value for a duplicated env name.
    for k, v in spec.extra_env.items():
        env.append({"name": k, "value": v})

    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    work_co_mount = bool(spec.data_pvc and spec.worktree_subpath)
    if work_co_mount:
        volumes.append({"name": "work", "persistentVolumeClaim": {"claimName": spec.data_pvc}})
        mounts.append({"name": "work", "mountPath": "/work", "subPath": spec.worktree_subpath})
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
        # #812 (Factory#274 compensating controls): pin the hardening in the
        # manifest instead of relying on the image alone. No
        # readOnlyRootFilesystem — nix writes /nix/var (builder db) and the
        # task writes $HOME on the rootfs, so the substrate can't tolerate it.
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    if mounts:
        container["volumeMounts"] = mounts

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        # #812: non-root enforced by the kubelet (not just the image USER) and
        # the default seccomp profile pinned.
        #
        # runAsUser is REQUIRED alongside runAsNonRoot (#848). #812 deliberately
        # omitted it — "the task images declare their own non-root uid" — but the
        # kubelet cannot verify a NAME. The build image declares `USER nonroot`,
        # so every build Job died before starting:
        #
        #   "container has runAsNonRoot and image has non-numeric user
        #    (nonroot), cannot verify user is non-root"
        #   -> the pod sticks in CreateContainerConfigError, forever.
        #
        # Observed live: 342 retries over 76 minutes, zero code written — the task
        # sat "in_progress" while nothing ran. (The gate path hit the SAME #812
        # premise from the other side: its image IS root, so it needs runAsNonRoot
        # dropped entirely — see kube_sandbox / #840.)
        #
        # 65532 is not a guess: `id` in the build image reports
        # uid=65532(nonroot) gid=65532(nonroot), and it is the uid the control
        # plane already creates worktree files as — so pinning it explicitly
        # changes no behaviour, it only lets the kubelet verify what was already
        # true. Keep runAsNonRoot: unlike the gate image, this one genuinely is
        # non-root and the hardening is worth having.
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": NONROOT_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
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
            "labels": job_labels(spec.service, spec.job_id),
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": spec.ttl_seconds,
            "activeDeadlineSeconds": spec.deadline_seconds,
            "template": {
                # The #812 NetworkPolicy label and the #1107 no-Service-collision
                # rule both live in task_pod_labels() — see its docstring. They are
                # constructed rather than spelled out here so that a consumer which
                # builds its OWN manifest (TFactory does) gets the identical labels
                # from the identical function instead of a hand-copy (Factory#483).
                "metadata": {"labels": task_pod_labels(spec.service)},
                "spec": pod_spec,
            },
        },
    }


def _selftest_npx_strip() -> None:
    """`npx playwright` must become the flake's own binary (TFactory#1152).

    Consumers emit "npx playwright test" because it is correct for the docker
    runner images. Under `nix develop` it is fatal: npx re-fetches a mismatched
    playwright from npm, that copy cannot resolve @playwright/test, and the lane
    reports zero tests — which reads as a flaky runner, not a provisioning bug.
    Proven live: `npx playwright test` found no tests while `playwright test` on
    the same shell passed in 6.5s.
    """
    wrapped = nix_develop_wrap(["npx playwright test tests/e2e"])
    _require(
        "'playwright test tests/e2e'" in wrapped or "playwright test tests/e2e" in wrapped,
        f"npx must be stripped for a flake-provided tool: {wrapped}",
    )
    _require("npx playwright" not in wrapped, f"npx survived the rewrite: {wrapped}")
    # Tools the flake does NOT ship keep their npx — stripping it there would turn
    # a working fetch into "command not found".
    keep = nix_develop_wrap(["npx cypress run"])
    _require("npx cypress run" in keep, f"npx must survive for a non-flake tool: {keep}")


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
        workspace_uri="s3://factory-artifacts/aifactory/482/proj-abc/workspace/workspace.tar.gz",
    )
    m = build_job_manifest(spec)
    name = m["metadata"]["name"]
    assert_job_policy(m)
    _require(name.startswith("factory-aifactory-"), f"name prefix: {name}")
    ps = m["spec"]["template"]["spec"]
    _require(ps["serviceAccountName"] == "aifactory-sandbox", "SA")
    _require(ps["automountServiceAccountToken"] is False, "no token automount")
    _require(
        ps["securityContext"]
        == {
            "runAsNonRoot": True,
            "runAsUser": NONROOT_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "pod securityContext (#812) with a NUMERIC uid (#848) — runAsNonRoot "
        "alone cannot verify the image's `USER nonroot` name and every build "
        "Job dies CreateContainerConfigError",
    )
    _require(m["metadata"]["labels"]["app"] == "aifactory", "Job object keeps app=service")
    # A NON-SERVING pod must not be selectable as a Service backend (#1107).
    # Checked as the RULE for EVERY dispatching service rather than against one
    # literal, so it still holds for any service added later (Factory#479).
    for svc in SERVICES:
        built = build_job_manifest(
            JobSpec(service=svc, job_id="s", commands=["true"], nix_develop=False)
        )
        assert_job_policy(built)
        pod = built["spec"]["template"]["metadata"]["labels"]
        _require(pod["app"] == f"{svc}-task", f"pod app label: {pod['app']}")
        _require(
            pod["factory.io/service"] == svc,
            "factory.io/service keeps the association `app` used to carry",
        )

    # Every rule assert_job_policy states needs a manifest that BREAKS it, or the
    # rule is unverified: feeding it only well-formed manifests passes whether the
    # check is there or not, and deleting the check would keep this self-test green.
    # Each entry below is a real way a hand-built manifest goes wrong.
    def _tf() -> dict[str, Any]:
        return build_job_manifest(
            JobSpec(service="tfactory", job_id="s", commands=["true"], nix_develop=False)
        )

    def _pod_labels(m: dict[str, Any]) -> Any:
        return m["spec"]["template"]["metadata"]["labels"]

    def _pod_spec(m: dict[str, Any]) -> Any:
        return m["spec"]["template"]["spec"]

    rejections: list[tuple[str, Any]] = [
        # The pod joins the tfactory Service and answers real traffic with
        # connection refused (TFactory#885 / AIFactory#1107 / Factory#458).
        ("pod app == its own service", lambda m: _pod_labels(m).__setitem__("app", "tfactory")),
        # Same defect via a DIFFERENT service's selector - a tfactory Job whose pod
        # is labelled app=minio joins the minio Service.
        ("pod app == another service", lambda m: _pod_labels(m).__setitem__("app", "aifactory")),
        # A consumer interpolating a raw run_id instead of _short()-ing it.
        ("non-DNS-1123 Job name", lambda m: m["metadata"].__setitem__("name", "factory-tf-Run_1")),
        ("Job name over 63 chars", lambda m: m["metadata"].__setitem__("name", "f" * 64)),
        # The pod matches no NetworkPolicy and runs unrestricted (#812).
        ("no factory.io/kind label", lambda m: _pod_labels(m).pop("factory.io/kind")),
        # Without it the app rule above cannot be checked at all.
        ("no factory.io/service label", lambda m: _pod_labels(m).pop("factory.io/service")),
        # A silent retry re-runs a task under the same job-state row (conventions 3).
        ("backoffLimit != 0", lambda m: m["spec"].__setitem__("backoffLimit", 3)),
        ("restartPolicy != Never", lambda m: _pod_spec(m).__setitem__("restartPolicy", "Always")),
        ("kind != Job", lambda m: m.__setitem__("kind", "Pod")),
    ]
    for why, break_it in rejections:
        bad = _tf()
        break_it(bad)
        try:
            assert_job_policy(bad)
        except AssertionError:
            continue
        raise AssertionError(f"assert_job_policy ACCEPTED a manifest with: {why}")
    # task_pod_labels itself must not be able to produce the colliding label.
    try:
        task_pod_labels("tfactory", role="")
    except AssertionError:
        pass
    else:
        raise AssertionError("task_pod_labels(role='') produced app == the service name")
    c = ps["containers"][0]
    _require(
        c["securityContext"]
        == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "container securityContext (#812)",
    )
    _require(c["image"] == DEFAULT_NIX_IMAGE, "nix-base image")
    _require("nix develop path:/work#default" in c["command"][2], "nix develop wrap")
    _require("go test ./..." in c["command"][2], "task command present")
    mount_paths = {mt["mountPath"] for mt in c["volumeMounts"]}
    _require(mount_paths == {"/work", "/nix/store"}, f"mounts: {mount_paths}")
    names = {e["name"] for e in c["env"]}
    _require(
        {"JOB_ID", "CORRELATION_KEY", "ARTIFACTS_URI", "WORKSPACE_URI", "FACTORY_SERVICE"} <= names,
        "env",
    )
    # No-store / no-worktree (PFactory light planning) still builds.
    bare = build_job_manifest(
        JobSpec(service="pfactory", job_id="s1", commands=["echo hi"], nix_develop=False)
    )
    bare_cmd = bare["spec"]["template"]["spec"]["containers"][0]["command"][2]
    _require(bare_cmd == "echo hi", "bare command (no nix wrap)")
    _require(
        "volumes" not in bare["spec"]["template"]["spec"],
        "no volumes when none requested",
    )
    # Factory#482: the packed-workspace branch is `elif spec.workspace_uri`, so the
    # fixture above — which sets the co-mount AND workspace_uri — never reaches it.
    # It is not a branch to get wrong: without the emptyDir, /work is the image's
    # root-owned dir and the nonroot unpack dies with `PermissionError: /work`,
    # caught only in live #190 validation.
    packed = build_job_manifest(
        JobSpec(
            service="aifactory",
            job_id="p1",
            commands=["true"],
            nix_develop=False,
            workspace_uri="s3://factory-artifacts/aifactory/1/p/workspace/workspace.tar.gz",
        )
    )["spec"]["template"]["spec"]
    _require(
        packed["volumes"] == [{"name": "work", "emptyDir": {}}],
        f"packed workspace -> writable emptyDir /work (no RWO co-mount): {packed.get('volumes')}",
    )
    _require(
        packed["containers"][0]["workingDir"] == "/work",
        "packed workspace still runs in /work",
    )
    _selftest_trace_env()
    sys.stdout.write(
        "job_dispatch self-test: PASS — manifest, policy rules (incl. the "
        "app-label and DNS-1123 rejections), nix-develop wrap, warm-store + "
        "worktree mounts, packed-workspace emptyDir, env, trace context "
        "(Factory#607), bare fallback\n"
    )


def _selftest_trace_env() -> None:
    """Factory#607. Each check below has a way to FAIL that has actually shipped."""
    saved = {
        k: os.environ.get(k) for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL")
    }

    def _env_of(service: str = "aifactory") -> dict[str, Any]:
        m = build_job_manifest(
            JobSpec(service=service, job_id="t1", commands=["true"], nix_develop=False)
        )
        return {e["name"]: e for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}

    try:
        # 1. No collector configured -> nothing added. A Job that carries OTLP
        #    config pointing at nothing burns its exporter's retry budget on
        #    every export for spans that can never land.
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        _require(trace_env("aifactory") == [], "endpoint unset must add no env")
        off = _env_of()
        _require(
            not [n for n in off if n.startswith("OTEL_") or n == "TRACEPARENT"],
            f"endpoint unset must leave the manifest OTEL-free: {sorted(off)}",
        )

        # 2. Collector configured -> the Job can export, and can say who it is.
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://observe:5080/api/default"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        on = _env_of()
        _require(
            on["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://observe:5080/api/default",
            "endpoint is passed through to the Job",
        )
        _require(
            on["OTEL_EXPORTER_OTLP_PROTOCOL"]["value"] == "http/protobuf",
            "protocol is passed through to the Job",
        )
        # The whole point: the collector must be able to tell a Job's spans from
        # its dispatcher's. Before Factory#607 no job-side service name had ever
        # appeared in the traces stream at all.
        _require(
            on["OTEL_SERVICE_NAME"]["value"] == "aifactory-job",
            f"job-side service name: {on['OTEL_SERVICE_NAME']}",
        )
        _require(
            _env_of("tfactory")["OTEL_SERVICE_NAME"]["value"] == "tfactory-job",
            "the job-side service name follows the dispatching service",
        )

        # 3. The credential is a REFERENCE. A literal here would be readable by
        #    anything that can `kubectl get job -o yaml`, and would freeze a value
        #    that observe-otlp-auth-sync rotates hourly (Factory#606).
        headers = on["OTEL_EXPORTER_OTLP_HEADERS"]
        _require(
            "value" not in headers
            and headers["valueFrom"]["secretKeyRef"]["name"] == OTLP_AUTH_SECRET,
            f"the OTLP credential must be a secretKeyRef, never a literal: {headers}",
        )
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "authorization=Basic SECRETVALUE"
        try:
            rendered = repr(_env_of())
            _require("SECRETVALUE" not in rendered, "the header VALUE leaked into the manifest")
        finally:
            os.environ.pop("OTEL_EXPORTER_OTLP_HEADERS", None)

        # 4. TRACEPARENT is present exactly when there is a trace to continue,
        #    and carries the ACTIVE span byte for byte. A Job handed a stale or
        #    fabricated traceparent still lands a span in the collector — under a
        #    trace nobody is looking at, which reads exactly like the fix working.
        #
        #    current_traceparent() is stubbed rather than driven by a real span:
        #    this self-test runs where OpenTelemetry is NOT installed (that is the
        #    point of the lazy import), so asking a real SDK for a span would make
        #    both halves of this check vacuously true — which is precisely how a
        #    "TRACEPARENT is injected" test passes against code that injects
        #    nothing.
        _require("TRACEPARENT" not in _env_of(), "no active span -> no TRACEPARENT")
        fake = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        real, globals()["current_traceparent"] = current_traceparent, lambda: fake
        try:
            _require(
                _env_of()["TRACEPARENT"]["value"] == fake,
                "the ACTIVE span's traceparent must reach the Job — without it the "
                "Job starts a new trace and the run's trace stops at this boundary "
                "(Factory#607)",
            )
        finally:
            globals()["current_traceparent"] = real

        # 5. extra_env still wins — a consumer must be able to override.
        m = build_job_manifest(
            JobSpec(
                service="aifactory",
                job_id="t1",
                commands=["true"],
                nix_develop=False,
                extra_env={"OTEL_SERVICE_NAME": "custom"},
            )
        )
        entries = m["spec"]["template"]["spec"]["containers"][0]["env"]
        # k8s takes the LAST value for a duplicated env name, so the override only
        # wins if extra_env is appended after trace_env. Assert the winning VALUE,
        # not the position: with the two blocks swapped the name is still last and
        # a position check passes while the override is silently discarded.
        winner = [e for e in entries if e["name"] == "OTEL_SERVICE_NAME"][-1]
        _require(
            winner.get("value") == "custom",
            f"extra_env must come AFTER trace_env so a consumer override wins: {entries}",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    _selftest()
    _selftest_npx_strip()
