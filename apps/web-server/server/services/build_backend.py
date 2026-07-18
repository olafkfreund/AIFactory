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

* Before the Job is created, the control plane POPULATES the per-task worktree
  (``populate_build_worktree`` — ``git worktree add`` + the spec materialized,
  reusing the in-pod ``core.workspace.setup_workspace`` path) at the data-PVC
  subPath the Job co-mounts at ``/work``. The control plane and the Job pod share
  the same single-node data PVC (RWO, co-mounted by subPath), so the worktree we
  write is exactly what the Job reads. Without it the Job saw an empty stub and
  run.py exited ``Spec '<id>' not found`` (#671). The RFC-0017 #207 pack/unpack is
  the multi-node path (PVC not shared); on this shared-PVC cluster, populating
  before dispatch is correct and simplest.
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
* The control plane reconciles a build from the **k8s Job's own status**
  (``reap_vanished_jobs`` → ``_job_outcome``): ``.status.succeeded`` → ``done``,
  ``.status.failed`` → ``failed``. ``backoffLimit: 0`` means one attempt, so the
  counts are unambiguous.

  #857 — this used to say "the Job writes its own job-state row (running →
  terminal); the control plane reconciles by polling Postgres". **That contract
  was never implemented.** ``mark_terminal`` exists only in the control plane
  (``job_state_store``); ``run.py`` has no job-state write and ``apps/backend``
  ships no code that could make one. So no build ever wrote a terminal row: the
  reaper waited out ``ttlSecondsAfterFinished`` (300s), found the Job GC'd, and
  marked every SUCCESSFUL build failed → ``human_review``. Reconciling from the
  Job object — which the reaper already fetched and discarded — needs no Job-side
  code, no ``DATABASE_URL`` in a container running agent-authored code, and no new
  build image. ``reconcile_by_poll`` still reads the durable row, which is now
  written by the control plane rather than awaited from the Job.
* Idempotent terminal reporting is keyed on (job_id, terminal-state) in the store
  (``mark_terminal`` is a harmless no-op on a second write) — so observing the
  same succeeded Job on several 15s ticks before its TTL expires is safe.
* The reaper still marks a ``running`` k8s-job row failed when its Job vanishes
  without ever being observed, or exceeds its deadline. Post-#857 that is a
  genuine anomaly (evicted / GC'd between ticks), not the everyday path.

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

#777: the build Job now also gets an ``install-clis`` initContainer (mirroring
the control-plane pod's provisioning) so non-``claude`` runtimes selectable via
``core/runtime_gating.py`` (``codex``, ``antigravity``/gemini) have their CLI
binary on PATH in the Job, not just a valid API credential. See
``_inject_install_clis``.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .task_phase import (
    _append_parallel_flags,
    _append_quick_mode_flag,
    should_pass_force,
)

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
# Where run.py lives inside the build image. This is the SAME var the in-pod
# path resolves (config.Settings.BACKEND_PATH is bound to APP_BACKEND_PATH), so
# the Job entrypoint references the identical run.py the in-pod subprocess does
# (agent_service._spawn_task_execution: ``self.backend_path / "run.py"``). The
# build Job runs on the aifactory runtime image (#671/#685), so this env is
# present in the Job container exactly as it is in the control-plane pod.
_ENV_BACKEND_PATH = "APP_BACKEND_PATH"

# RFC-0017 Stage E (#190) — multi-node workspace handoff. When ON, the control
# plane packs the populated build worktree to object storage and sets the Job's
# ``workspace_uri`` (→ ``WORKSPACE_URI`` env); the Job unpacks it into ``/work``
# instead of relying on the RWO co-mount that pins it to one node. Default OFF:
# the #671 co-mount path is unchanged. The producer is INERT until this flag is
# set AND S3_* is provisioned, and the Job-side consumer (unpack) lands in a
# separate slice — so an emitted ``WORKSPACE_URI`` is harmless until both are on.
_ENV_PACK_WORKSPACE = "AIFACTORY_PACK_WORKSPACE"
# RFC-0017 #190: when the build image ships its own ``/nix/store`` (the ``-nix``
# build-image variant), the node-pinned RWO ``aifactory-nix-store`` warm-cache
# PVC must NOT be co-mounted on the packed path — that mount both re-pins the
# Job to the PVC's single node AND masks the image's baked store. This flag
# The nix-source flag itself now lives in core.nix_env (both Job paths read it).

# Defaults mirror gate_runner.py + the kube_sandbox SA.
_DEFAULT_REPO_PVC = "aifactory-data"
_DEFAULT_DATA_ROOT = "/home/nonroot/.aifactory"
_DEFAULT_NAMESPACE = "factory"
_DEFAULT_SERVICE_ACCOUNT = "aifactory-sandbox"
_DEFAULT_DEADLINE_SECONDS = 6 * 3600  # a full build is long-lived
# Default backend dir = the image's project layout (Dockerfile sets
# APP_BACKEND_PATH=/home/projects/MagesticAI/apps/backend). Only used as a
# fallback when the env is somehow unset; the image always sets it.
_DEFAULT_BACKEND_PATH = "/home/projects/MagesticAI/apps/backend"

# The worktree a build runs in (mirrors agent_service._spawn_task_execution).
_WORKTREE_TEMPLATE = ".aifactory/worktrees/tasks/{spec_id}"

# -- build Job environment (#671 OAuth-env defect) --------------------------- #
#
# A dispatched build Job is a FRESH pod: it inherits NONE of the control-plane
# pod's ``os.environ``. The in-pod subprocess path, by contrast, runs run.py with
# ~37 env vars (``make_subprocess_env`` + the explicit overrides in
# ``agent_service._spawn_task_execution`` + the pooled OAuth token). Without
# mirroring that env into the Job container, run.py started but died
# ``Error: No OAuth token found`` (cli/utils.validate_environment): the Job had
# only JOB_ID + FACTORY_SERVICE.
#
# This builds the build-only env the Job needs, mirroring the in-pod construction:
#   * The pooled OAuth token (``CLAUDE_CODE_OAUTH_TOKEN``) — the critical fix,
#     resolved by the CALLER from the same token pool the in-pod path uses (#670)
#     so concurrent Jobs draw DISTINCT tokens. Passed in, never read here.
#   * The provider/runtime SDK env the agent legitimately needs — the SAME
#     allowlist core/auth.py keeps for the agent subprocess (``_AGENT_ENV_KEEP``:
#     ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / model overrides / SDK behaviour)
#     — but ONLY when present in the control-plane env (a fresh Job shouldn't get
#     empty placeholders).
#   * GITHUB_TOKEN / GH_TOKEN when present — run.py uses these for the PR endgame,
#     exactly as the in-pod build does. (run.py re-applies core/auth.py's agent
#     scrubbing to its OWN agent sub-subprocess, so this mirrors in-pod scope.)
#   * The fixed non-interactive build env the in-pod path sets verbatim
#     (CLAUDE_CODE_ENTRYPOINT/CI/PYTHONUNBUFFERED/PYTHONIOENCODING).
#
# Deliberately EXCLUDED to keep the Job scoped to the build env (no unrelated
# secret leak): ANTHROPIC_API_KEY* (AIFactory's OAuth-only policy — never bill the
# direct-API account), and control-plane secrets run.py doesn't use (DATABASE_URL,
# JWT_SECRET, API_TOKEN, …; run.py writes no job-state row, so it needs no DB URL).
#
# SECURITY (#599 class): every value goes into the Job container ``env`` (via the
# shared job_dispatch builder, which renders extra_env as ``{name,value}`` list
# entries — NOT into ``command``/argv), so the token is never ps-visible in the
# pod's argv. There is no chart-managed Secret carrying the OAuth token to point a
# ``secretKeyRef`` at — the token only exists as a runtime-resolved value in the
# control plane (the pool resolves it from the profiles file on the data PVC / env
# / file at dispatch time), so it is injected as a resolved ``env`` literal. It is
# kept out of argv and out of logs; the literal lives only in the Job manifest /
# the Job pod's env (the same trust boundary the in-pod subprocess env already
# crosses), and the Job is TTL-GC'd shortly after it finishes.

# Fixed non-interactive build env — byte-identical to the explicit overrides the
# in-pod path sets in agent_service._spawn_task_execution.
_FIXED_BUILD_ENV: dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CI": "true",
}

# Provider/runtime env to propagate WHEN PRESENT in the control-plane env. Mirrors
# core/auth.py::_AGENT_ENV_KEEP (the SDK passthrough the agent legitimately needs)
# plus GITHUB_TOKEN/GH_TOKEN for run.py's PR endgame. ANTHROPIC_API_KEY is NOT
# here — OAuth-only policy (subprocess_env._STRIP_VARS).
_PASSTHROUGH_BUILD_ENV: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "NO_PROXY",
    "DISABLE_TELEMETRY",
    "DISABLE_COST_WARNINGS",
    "API_TIMEOUT_MS",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    # Non-Claude provider credentials + config a non-Claude-routed build resolves
    # from env (byo_llm.py / phase routing). Mirrors TFactory #480's verify-Job
    # provider set; forwarded ONLY when present (the loop below skips empties), in
    # env (never argv), per #689. ANTHROPIC_API_KEY stays excluded (OAuth-only).
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "GEMINI_API_KEY",
    "GEMINI_CLI_TRUST_WORKSPACE",
    "GOOGLE_API_KEY",
    "OLLAMA_API_KEY",
    "OLLAMA_CLOUD_BASE_URL",
    # RFC-0017 #190: the Job-side consumer (core/workspace_fetch.maybe_unpack_workspace)
    # reconstitutes /work from object storage via core/artifact_store, which reads this
    # S3_* namespace (NOT the chart's fsspec AIFACTORY_S3_* / AWS_* WorkspaceStore vars).
    # Without these in the Job env a packed-workspace build (WORKSPACE_URI set) cannot
    # reach the bucket and dies fail-loud on unpack. Forwarded ONLY when present — so
    # inert when S3 isn't configured (AIFACTORY_PACK_WORKSPACE off / single-node
    # co-mount path), in env never argv, matching the credential rule above.
    "S3_ENDPOINT",
    "S3_BUCKET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_REGION",
    # #804: the coder block in core/client.py reads AIFACTORY_GRAPHIFY_ENABLED at
    # build time to opt into the graphify code-graph MCP tool. In a kubejob build
    # the coder runs in a fresh Job pod, so the Deployment's flag only reaches it
    # if forwarded here. Forwarded ONLY when present (== "true" on the Deployment);
    # the graphify tooling (graphifyy[mcp]) is baked into the build-Job image.
    "AIFACTORY_GRAPHIFY_ENABLED",
)


def build_job_env(oauth_token: str | None) -> dict[str, str]:
    """Resolve the env the build Job container needs (#671 OAuth-env defect).

    Mirrors the in-pod env construction so the Job behaves like the in-pod build:
    the pooled OAuth token (passed in by the caller — see module note above),
    the fixed non-interactive build env, and the provider/runtime SDK env that is
    actually present in the control-plane environment. Pure + side-effect free so
    it is unit-testable with a mocked ``os.environ``.

    The OAuth token is the load-bearing fix: a fresh Job pod has no credential
    source on PATH, so without ``CLAUDE_CODE_OAUTH_TOKEN`` run.py dies
    ``No OAuth token found``. ``ANTHROPIC_API_KEY`` is never included (OAuth-only
    policy); control-plane secrets run.py doesn't use are never included (scope).
    """
    env: dict[str, str] = dict(_FIXED_BUILD_ENV)
    for name in _PASSTHROUGH_BUILD_ENV:
        val = os.environ.get(name)
        if val:  # only propagate real values; never empty placeholders
            env[name] = val
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


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
        _ENV_BACKEND,
        raw,
        BACKEND_SUBPROCESS,
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
    return norm[len(root) :]


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
        _ENV_BUILD_IMAGE,
        _ENV_RUNNING_IMAGE,
        default_nix_image,
        _ENV_BUILD_IMAGE,
        _ENV_RUNNING_IMAGE,
    )
    return default_nix_image


def _resolve_run_py_path() -> str:
    """Resolve the ABSOLUTE path to run.py inside the build image (#671 defect).

    The build Job co-mounts the worktree at ``/work`` and runs there, but run.py
    does NOT live in the worktree — it lives in the backend dir baked into the
    image (``APP_BACKEND_PATH``). A bare ``python run.py`` from ``/work`` fails
    with ``can't open file '/work/run.py': [Errno 2]`` because run.py isn't
    there. Using the absolute backend path makes run.py resolvable while keeping
    ``--project-dir /work`` as the project the build operates on — mirroring the
    in-pod invocation ``[sys.executable, self.backend_path / "run.py", …]`` in
    ``agent_service._spawn_task_execution``.
    """
    backend_path = (
        os.environ.get(_ENV_BACKEND_PATH, "").strip() or _DEFAULT_BACKEND_PATH
    )
    # PurePosix join: the Job container is Linux regardless of where the manifest
    # is built, so always emit a forward-slash path.
    return f"{backend_path.rstrip('/')}/run.py"


# -- file-auth CLI credential seeding (#690) --------------------------------- #
#
# Some providers authenticate via credential FILES (codex/gemini-oauth/copilot),
# seeded in-pod on the control plane by a ``seed-creds`` initContainer that
# copies the ``factory-cli-creds`` secret into the home dirs. A FRESH build Job
# pod has none of these, so a build routed to a file-auth provider fails in-Job.
# This mirrors that seeding into the dispatched Job pod, OPT-IN via a configured
# secret name (default off → dev/test without the secret are unaffected, and the
# env-auth path #688/#689 is unchanged). The control plane sets
# AIFACTORY_CLI_CREDS_SECRET=factory-cli-creds.
_ENV_CLI_CREDS_SECRET = "AIFACTORY_CLI_CREDS_SECRET"
# Build image HOME (Dockerfile ``USER nonroot``) — where the CLIs look for creds.
_BUILD_HOME = "/home/nonroot"
# (secret key in the cli-creds secret  ->  path under HOME). Mirrors the
# control-plane seed-creds initContainer in factory-gitops.
_CLI_CRED_FILES: tuple[tuple[str, str], ...] = (
    ("claude-credentials.json", ".claude/.credentials.json"),
    ("codex-auth.json", ".codex/auth.json"),
    ("copilot-apps.json", ".config/github-copilot/apps.json"),
    ("gemini-oauth_creds.json", ".gemini/oauth_creds.json"),
)
# emptyDir home dirs shared between the seed initContainer and the build container.
_SEED_HOME_VOLUMES: tuple[tuple[str, str], ...] = (
    ("cc-claude", f"{_BUILD_HOME}/.claude"),
    ("cc-codex", f"{_BUILD_HOME}/.codex"),
    ("cc-gemini", f"{_BUILD_HOME}/.gemini"),
    ("cc-config", f"{_BUILD_HOME}/.config"),
)


# -- provider CLI provisioning (#777) ----------------------------------------- #
#
# The coding phase can select the ``codex`` runtime (core/runtime_gating.py), whose
# provider spawns the ``codex`` CLI binary directly (not just the API). The
# control-plane Deployment provisions ``claude``/``codex``/``gemini`` (+ the
# ``antigravity`` alias) into a shared ``/clis`` emptyDir via an ``install-clis``
# initContainer (factory-gitops apps/aifactory/manifests/manifests.yaml) and
# prepends ``/clis/bin`` to ``PATH``. The dispatched build Job is a FRESH pod that
# never got this treatment, so a build routed to ``codex`` died ``Fatal error:
# Codex CLI executable not found: 'codex'`` even though OPENAI_API_KEY was valid —
# the CLI just was not on PATH. This mirrors that SAME provisioning into the build
# Job pod, unconditionally (the control plane always runs it on every pod start,
# so the build Job does too — no opt-in flag). The -nix build image bakes
# ``claude`` already (a claude build works), but not ``codex``/``gemini`` — this
# closes that gap for every runtime.
_INSTALL_CLIS_IMAGE = "node:22-bookworm-slim"
_INSTALL_CLIS_SCRIPT = (
    "set -e\n"
    "export npm_config_prefix=/clis\n"
    "npm install -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli\n"
    # antigravity CLI == gemini-cli, invoked as `antigravity` (matches gitops).
    "ln -sf /clis/bin/gemini /clis/bin/antigravity\n"
)
_CLIS_VOLUME_NAME = "clis"
_CLIS_MOUNT_PATH = "/clis"
# Prepends /clis/bin to the -nix BUILD image's own default PATH (Dockerfile
# build-runtime stage), NOT the control-plane Deployment's — the build image
# additionally carries /nix/var/nix/profiles/default/bin (where ``nix`` lives; the
# build runs ``nix develop`` for the SUT toolchain) and /home/nonroot/.npm-global/bin.
# Dropping either breaks the packed build, so keep the image's exact PATH and only
# prepend /clis/bin for the provisioned provider CLIs.
_BUILD_PATH_ENV = (
    "/clis/bin:/nix/var/nix/profiles/default/bin:/home/nonroot/.npm-global/bin:"
    "/home/projects/MagesticAI/.venv/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/bin:/usr/sbin:/sbin:/bin"
)


def _init_container_security_context(run_as_user: int) -> dict[str, Any]:
    """#812: the shared job_dispatch builder sets pod-level ``runAsNonRoot``;
    the injected initContainer images (busybox, node) default to root, so each
    must pin an explicit non-root uid or the kubelet rejects the pod. Same
    escalation/capability hardening as the task container."""
    return {
        "runAsUser": run_as_user,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def _inject_seed_creds(manifest: dict[str, Any]) -> dict[str, Any]:
    """Add a ``seed-creds`` initContainer that materializes the file-auth CLI
    credentials into the build Job pod (#690). No-op unless a secret name is
    configured. Mutates + returns the manifest. Pure (env-only) → unit-testable.
    """
    secret = os.environ.get(_ENV_CLI_CREDS_SECRET, "").strip()
    if not secret:
        return manifest  # default off: env-auth-only path unchanged
    pod = manifest["spec"]["template"]["spec"]
    home_mounts = [{"name": n, "mountPath": p} for n, p in _SEED_HOME_VOLUMES]

    volumes = pod.setdefault("volumes", [])
    for name, _path in _SEED_HOME_VOLUMES:
        volumes.append({"name": name, "emptyDir": {}})
    volumes.append({"name": "cli-creds", "secret": {"secretName": secret}})

    # cp is tolerant (a partial secret must not fail the whole build): only copy
    # files that exist, and never let a missing one abort via ``set -e``.
    mkdirs = " ".join(
        f"{_BUILD_HOME}/{d}"
        for d in (".claude", ".codex", ".gemini", ".config/github-copilot")
    )
    lines = [f"mkdir -p {mkdirs}"]
    lines += [
        f"[ -f /seed/{key} ] && cp /seed/{key} {_BUILD_HOME}/{rel} || true"
        for key, rel in _CLI_CRED_FILES
    ]
    lines.append(
        f"chmod -R g+rwX {_BUILD_HOME}/.claude {_BUILD_HOME}/.codex "
        f"{_BUILD_HOME}/.gemini {_BUILD_HOME}/.config || true"
    )
    pod.setdefault("initContainers", []).append(
        {
            "name": "seed-creds",
            "image": "busybox:1.36",
            "command": ["sh", "-c"],
            "args": ["\n".join(lines)],
            # Build-image nonroot uid (65532): copied cred files land owned by
            # the uid the task container runs as; emptyDirs are world-writable.
            "securityContext": _init_container_security_context(65532),
            "volumeMounts": [
                *home_mounts,
                {"name": "cli-creds", "mountPath": "/seed", "readOnly": True},
            ],
        }
    )
    pod["containers"][0].setdefault("volumeMounts", []).extend(home_mounts)
    return manifest


def _inject_install_clis(manifest: dict[str, Any]) -> dict[str, Any]:
    """Add the ``install-clis`` initContainer so the build Job has the same
    provider CLIs (``claude``/``codex``/``gemini``/``antigravity``) on PATH that
    the control-plane pod is provisioned with (#777). Unconditional — mirrors the
    control plane, which runs this on every pod start with no opt-in flag.
    Mutates + returns the manifest. Pure (no env / no I/O) → unit-testable.
    """
    pod = manifest["spec"]["template"]["spec"]
    pod.setdefault("volumes", []).append({"name": _CLIS_VOLUME_NAME, "emptyDir": {}})
    pod.setdefault("initContainers", []).insert(
        0,
        {
            "name": "install-clis",
            "image": _INSTALL_CLIS_IMAGE,
            "command": ["sh", "-c"],
            "args": [_INSTALL_CLIS_SCRIPT],
            # node image's "node" user (uid 1000): HOME=/home/node stays
            # writable for the npm cache; /clis is a world-writable emptyDir.
            "securityContext": _init_container_security_context(1000),
            "volumeMounts": [
                {"name": _CLIS_VOLUME_NAME, "mountPath": _CLIS_MOUNT_PATH}
            ],
        },
    )
    container = pod["containers"][0]
    container.setdefault("volumeMounts", []).append(
        {"name": _CLIS_VOLUME_NAME, "mountPath": _CLIS_MOUNT_PATH}
    )
    container.setdefault("env", []).append({"name": "PATH", "value": _BUILD_PATH_ENV})
    return manifest


def build_run_py_job_manifest(
    *,
    task_id: str,
    project_path: Path,
    spec_id: str,
    correlation_key: str | None = None,
    extra_env: dict[str, str] | None = None,
    workspace_uri: str | None = None,
    stop_after_planning: bool = False,
    parallel: bool | None = None,
    workers: int | None = None,
    force: bool = False,
    base_branch: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Build the k8s Job manifest that runs ``run.py`` for one build (#671).

    Pure (no cluster access) so it is unit-testable. Delegates the manifest
    shape to the shared ``job_dispatch`` builder (SA, warm store, worktree
    co-mount, no-retry, TTL, deadline) and supplies the AIFactory-specific
    entrypoint:

        python <backend>/run.py --spec <spec_id> --project-dir /work \
            --auto-continue [--force]

    ``--force`` is conditional (#916): it is passed when the spec has no review
    requirement (the headless default) or when ``force`` is set because the plan
    was manually approved — the same rule the in-pod path applies. It is NOT
    "pure": ``should_pass_force`` reads the spec's task_metadata/requirements
    from ``project_path``, and syncs them, exactly as the in-pod path does.

    run.py is referenced by its ABSOLUTE path inside the image
    (``_resolve_run_py_path`` → ``APP_BACKEND_PATH``), NOT a bare ``run.py``
    resolved against the ``/work`` worktree — run.py lives in the image's backend
    dir, not the co-mounted worktree, so ``python run.py`` from ``/work`` died
    with ``can't open file '/work/run.py'`` (#671). ``--project-dir /work`` keeps
    the build operating on the worktree. This mirrors the in-pod invocation
    ``[sys.executable, self.backend_path / "run.py", "--spec", …, "--project-dir",
    …]`` in ``agent_service._spawn_task_execution``.

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
    # RFC-0017 #190: on the packed (multi-node) path, the warm-store PVC is a
    # second RWO local-path pin (and would mask the image's baked /nix/store), so
    # drop it when the build image carries nix. Gated (default OFF); the co-mount
    # and non-packed paths keep the warm store. See _packed_nix_in_image.
    if workspace_uri and _packed_nix_in_image():
        nix_store_pvc = None
    data_root = os.environ.get(_ENV_DATA_ROOT, _DEFAULT_DATA_ROOT)
    namespace = (
        os.environ.get(_ENV_NAMESPACE, _DEFAULT_NAMESPACE).strip() or _DEFAULT_NAMESPACE
    )
    service_account = (
        os.environ.get(_ENV_SERVICE_ACCOUNT, _DEFAULT_SERVICE_ACCOUNT).strip()
        or _DEFAULT_SERVICE_ACCOUNT
    )
    try:
        deadline = int(os.environ.get(_ENV_DEADLINE, _DEFAULT_DEADLINE_SECONDS))
    except (TypeError, ValueError):
        deadline = _DEFAULT_DEADLINE_SECONDS

    # RFC-0017 #190: a packed-workspace Job (workspace_uri set) unpacks /work from
    # object storage, so it must NOT co-mount the RWO worktree subPath — that
    # co-mount is exactly what pins a Job to the worktree's single node. Dropping
    # it here (→ data_pvc=None below) is what makes multi-node scheduling possible.
    # When workspace_uri is None (default), the #671 co-mount path is unchanged.
    worktree_subpath = (
        None
        if workspace_uri
        else (_worktree_subpath(data_root, project_path, spec_id) if repo_pvc else None)
    )

    # The build entrypoint: run.py (absolute path in the image) against the
    # co-mounted worktree at /work. Mirrors agent_service._spawn_task_execution's
    # headless invocation argv shape (run.py abs path, --spec, --project-dir,
    # --auto-continue, conditional --force). run.py lives in the image's backend dir, NOT the
    # worktree, so it MUST be referenced absolutely — a bare ``run.py`` resolved
    # against /work died with ``can't open file '/work/run.py'`` (#671). NOT
    # nix-develop-wrapped (nix_develop=False) — run.py drives its own per-task
    # env; the entrypoint just needs an interpreter, which the aifactory build
    # image (resolved above) provides.
    run_py = _resolve_run_py_path()
    argv = [
        "python",
        run_py,
        "--spec",
        spec_id,
        "--project-dir",
        "/work",
        "--auto-continue",
    ]
    # #916: --force used to be hardcoded here, on EVERY manifest. That made the
    # flag a statement about nothing rather than about the caller's intent: a
    # review-gated task passed run.py's pre-flight gate (noisily) and only
    # stopped later, at the coder's own gate. That coder gate is the ONLY thing
    # that kept the hardcode harmless — it takes no ``force`` parameter, so the
    # flag cannot reach it — which left a landmine: wire ``force`` into
    # ``coder.run()`` and this path silently becomes a real approval bypass.
    # Derive the flag from the same helper the in-pod path uses instead, so
    # ``requireReviewBeforeCoding`` is honoured in the argv itself.
    if should_pass_force(_spec_source_dir(project_path, spec_id), force):
        argv.append("--force")
    # --stop-after-planning mirrors the in-pod path (cli/main.py): the kubejob
    # backend previously dropped this flag, so a planning-only request silently
    # ran the full build (coder + QA + PR). Thread it into the Job's run.py argv.
    if stop_after_planning:
        argv.append("--stop-after-planning")
    # #916: --base-branch mirrors the in-pod path. The caller's override (the
    # /start payload's baseBranch) used to stop at _start_build_unit, so run.py
    # in the Job always auto-detected the base and built from the default branch.
    if base_branch:
        argv.extend(["--base-branch", base_branch])
    # #916: quick mode mirrors the in-pod path — --skip-qa in argv (the QA loop
    # is skipped; the quick coder prompt validates inline) + QUICK_MODE=true in
    # the container env (prompts_pkg reads the env to pick the quick prompts).
    # Both used to be dropped, so a quick task ran the full pipeline in the Job.
    if _append_quick_mode_flag(argv, mode):
        extra_env = {**(extra_env or {}), "QUICK_MODE": "true"}
    # #914: same defect class for the #376 parallel harness. The resolved
    # execution reached here (task_metadata said ``"parallel": true``) but the
    # Job's argv never carried ``--parallel``, so EVERY kubejob build ran serial
    # — and #671 made kubejob the live default, leaving the wave harness inert on
    # the cluster for intake labels, the portal setting AND PFactory-planned
    # contracts alike. Reuse the in-pod helper rather than re-deriving the flags:
    # a second copy of the "--parallel [--workers N]" rule is exactly how this
    # feature's three worker constants already drifted apart.
    _append_parallel_flags(argv, parallel, workers)
    commands = [" ".join(argv)]

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
        # RFC-0017 #190: when the producer packed the worktree, the Job receives
        # WORKSPACE_URI and unpacks it into /work (multi-node). None (default) →
        # WORKSPACE_URI is omitted and the /work co-mount path is unchanged.
        workspace_uri=workspace_uri,
    )
    return _inject_install_clis(_inject_seed_creds(build_job_manifest(spec)))


def _spec_source_dir(project_path: Path, spec_id: str) -> Path:
    """Where the authored spec lives in the project (mirrors the in-pod path).

    The in-pod build resolves the spec under ``<project>/.aifactory/specs/<id>``
    (``cli.utils.find_spec`` / ``agent_service._spawn_task_execution``), copies it
    into the worktree, and runs run.py from there. The build Job needs the SAME
    source dir to materialize into the co-mounted worktree before dispatch.
    """
    return project_path / ".aifactory" / "specs" / spec_id


def populate_build_worktree(project_path: Path, spec_id: str) -> str | None:
    """Create + populate the per-task build worktree BEFORE the Job is created.

    THE #671 build-worktree defect fix. The kubejob dispatch co-mounts the data
    PVC's worktree subPath (``.aifactory/worktrees/tasks/<id>``) at the Job's
    ``/work``, but that subPath was never populated: the Job saw an EMPTY STUB
    (only ``.aifactory/.gitignore_checked`` + ``.gitignore``, no git checkout and
    no ``.aifactory/specs/<id>/spec.md``), so run.py exited
    ``Spec '<id>' not found / No specs found``.

    The in-pod subprocess path runs ``run.py --project-dir <real project>``: run.py
    itself calls ``core.workspace.setup.setup_workspace`` →
    ``WorktreeManager.create_worktree`` (``git worktree add``) +
    ``copy_spec_to_worktree`` to build a REAL worktree with the spec inside. The
    Job path instead runs ``run.py --project-dir /work`` where ``/work`` is *only*
    the worktree subPath — so nothing ever populates it.

    Shared-PVC reasoning: on this single-node cluster the control-plane pod and the
    Job pod share the SAME data PVC (RWO, co-mounted by subPath), so the control
    plane can populate the worktree on disk and the Job will see it through the
    same PVC. We therefore reuse the EXACT in-pod preparation
    (``setup_workspace`` in ISOLATED mode, ``source_spec_dir`` = the authored spec
    dir) here, before ``create_namespaced_job``. The worktree it creates lands at
    ``<project>/.aifactory/worktrees/tasks/<id>`` — byte-for-byte the subPath the
    manifest co-mounts at ``/work`` (see ``_worktree_subpath`` /
    ``WorktreeManager.get_worktree_path``) — so the Job then sees a populated
    ``/work`` and run.py finds the spec.

    (The RFC-0017 #207 workspace pack/unpack is the MULTI-NODE path, for clusters
    where the PVC is NOT shared between control plane and Job; on this shared-PVC
    single-node cluster, populating before dispatch is the correct + simplest fix
    and avoids a pack/unpack round-trip entirely.)

    Returns the absolute worktree path that was populated (for logging/tests), or
    ``None`` when the worktree is outside the data PVC (dev/test on a laptop, where
    the manifest also skips the co-mount — there is nothing to populate). Reusing
    the in-pod code means materialization stays identical to the subprocess path;
    we do NOT reinvent worktree creation or spec copying here.
    """
    data_root = os.environ.get(_ENV_DATA_ROOT, _DEFAULT_DATA_ROOT)
    repo_pvc = os.environ.get(_ENV_REPO_PVC, _DEFAULT_REPO_PVC).strip() or None
    # Only populate when the Job will actually co-mount the worktree (same gate the
    # manifest uses): no PVC / outside the data root → no /work mount → nothing to
    # populate (and no shared PVC to populate it on).
    if repo_pvc is None or _worktree_subpath(data_root, project_path, spec_id) is None:
        _log.info(
            "[build_backend] worktree for %s is outside the data PVC — skipping "
            "pre-dispatch population (the Job has no /work co-mount)",
            spec_id,
        )
        return None

    source_spec_dir = _spec_source_dir(project_path, spec_id)
    if not source_spec_dir.exists():
        # The spec must already be authored under the project before a build can
        # run. Fail loudly rather than dispatch a Job that will hit the very
        # "Spec not found" this fix exists to prevent.
        raise FileNotFoundError(
            f"[build_backend] cannot dispatch build for {spec_id}: authored spec "
            f"dir {source_spec_dir} does not exist (nothing to materialize into "
            "the build worktree)"
        )
    return _populate_self_contained_worktree(project_path, spec_id, source_spec_dir)


def _pack_workspace_enabled() -> bool:
    """RFC-0017 #190 producer gate. Default OFF — the co-mount path is unchanged."""
    return os.environ.get(_ENV_PACK_WORKSPACE, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _packed_nix_in_image() -> bool:
    """RFC-0017 #190 nix-source gate. Default OFF.

    When ON, a packed build Job (``workspace_uri`` set) drops the node-pinned
    ``aifactory-nix-store`` warm-cache PVC and uses the ``/nix/store`` baked into
    the build image instead, making the Job node-agnostic. Only meaningful once
    ``AIFACTORY_BUILD_IMAGE`` points at the nix-baked ``-nix`` image; flip both
    together in gitops. Off → the warm-store co-mount is unchanged.

    Delegates to ``core.nix_env.nix_in_image`` so the build and gate paths read
    one predicate — two copies is how the gate path missed the #258 flip (#253).
    """
    from core.nix_env import (
        nix_in_image,  # noqa: PLC0415 - core is a startup sys.path add
    )

    return nix_in_image()


def _maybe_pack_workspace(
    worktree_path: str | None,
    *,
    task_id: str,
    correlation_key: str | None,
) -> str | None:
    """Pack the populated build worktree to object storage (RFC-0017 #190).

    Returns the ``s3://`` workspace-archive URI to thread onto the Job's
    ``workspace_uri`` (→ ``WORKSPACE_URI`` env), or ``None`` when the producer is
    OFF, there is nothing to pack, or packing failed. A pack failure is logged and
    swallowed: the Job still has the RWO ``/work`` co-mount to fall back on, so a
    transient object-store error never strands a dispatch. INERT by default — the
    flag is off and the Job-side unpack lands in a later slice, so an emitted URI
    is harmless until both sides are on.
    """
    if not _pack_workspace_enabled() or not worktree_path:
        return None
    try:
        # Deferred import — the vendored client lives on the backend path (core.*),
        # added to sys.path at startup (same pattern as the job_dispatch builder).
        from core.artifact_store import ArtifactRef, ArtifactStore, pack_workspace

        ref = ArtifactRef(
            service="aifactory",
            job_id=task_id,
            role="workspace",
            correlation_key=correlation_key,
        )
        uri = pack_workspace(ArtifactStore(), ref, worktree_path)
        _log.info(
            "[build_backend] packed build worktree for %s -> %s (RFC-0017 #190)",
            task_id,
            uri,
        )
        return uri
    except Exception:  # noqa: BLE001 — a pack error must never break dispatch
        _log.warning(
            "[build_backend] workspace pack failed for %s; falling back to the "
            "/work co-mount (RFC-0017 #190)",
            task_id,
            exc_info=True,
        )
        return None


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], check=check, capture_output=True, text=True, timeout=300
    )


def _git_out(args: list[str]) -> str:
    """git stdout, or '' on failure (never raises — best-effort reads)."""
    r = _git(args, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _populate_self_contained_worktree(
    project_path: Path, spec_id: str, source_spec_dir: Path
) -> str:
    """Build ``/work`` as a STANDALONE git repo for the dispatched build Job (#671).

    The original #671 fix used ``setup_workspace(ISOLATED)`` → ``git worktree add``,
    which makes the worktree's ``.git`` a FILE pointing at
    ``<project>/.git/worktrees/<id>``. The Job co-mounts ONLY the worktree subPath
    at ``/work`` — the project's real ``.git`` is not mounted — so that gitdir
    pointer dangles in the Job pod and every git op (WorktreeManager, the
    PR-endgame push) fails (the build-default flip blocker).

    Instead, build a self-contained local clone: a real ``.git`` directory, the
    task branch checked out, the GitHub ``origin`` set, and the spec materialized.
    Reuses the project's own conventions so ``/work`` matches the in-pod worktree
    shape: ``WorktreeManager.get_branch_name`` / ``get_worktree_path`` (so the
    branch + path are byte-identical to the subPath the manifest co-mounts) and
    ``copy_spec_to_worktree`` (the same gitignored spec materialization run.py
    does in-pod). Inert until ``AIFACTORY_BUILD_BACKEND=kubejob`` (default off).
    """
    worktree_mod: Any = importlib.import_module("core.worktree")
    workspace: Any = importlib.import_module("core.workspace")

    manager: Any = worktree_mod.WorktreeManager(project_path)
    branch = manager.get_branch_name(spec_id)
    wt_path = Path(manager.get_worktree_path(spec_id))
    base_branch = manager.base_branch

    # The real GitHub remote the build pushes to (the local clone source below is
    # not reachable from the Job pod, so origin must point at GitHub).
    origin = _git_out(["-C", str(project_path), "remote", "get-url", "origin"])

    # Fresh standalone clone: a real .git directory with independent objects
    # (--no-hardlinks → self-contained even though it clones a local path), at the
    # base branch. Replaces any stale worktree dir at the same path.
    if wt_path.exists():
        shutil.rmtree(wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git(
        [
            "clone",
            "--local",
            "--no-hardlinks",
            "--branch",
            base_branch,
            str(project_path),
            str(wt_path),
        ]
    )
    # IMPORTANT (#716): leave /work on the BASE branch — do NOT pre-create or
    # check out the aifactory/<spec> task branch here. run.py's own setup_workspace
    # (WorktreeManager.create_worktree) creates that worktree + branch FROM the base
    # branch inside the Job, exactly as it does in-pod. Pre-checking it out makes
    # run.py's `git worktree add -b aifactory/<spec>` fail ("branch already
    # exists") → WorktreeError, crashing the build. The clone only needs to be a
    # self-contained repo (real .git + GitHub origin + the spec) on the base branch.
    if origin:
        _git(["-C", str(wt_path), "remote", "set-url", "origin", origin])
    # Materialize the spec into the clone working tree (gitignored/uncommitted —
    # exactly as the in-pod worktree carries it).
    workspace.copy_spec_to_worktree(source_spec_dir, wt_path, spec_id)

    populated = str(wt_path)
    _log.info(
        "[build_backend] built self-contained build repo for %s at %s "
        "(base=%s, origin=%s; run.py creates the %s worktree in-Job) before dispatch",
        spec_id,
        populated,
        base_branch,
        "set" if origin else "unset",
        branch,
    )
    return populated


class KubeJobBuildBackend:
    """Dispatch + reconcile + reap the run.py Job (RFC-0016 #671).

    Cluster I/O is confined to the ``_apply`` / ``_job_outcome`` / ``_delete``
    helpers, each taking an injectable ``batch`` client so tests pass a fake.
    The orchestration (build manifest → apply → record worker_ref →
    reconcile-by-poll → reap) is pure-ish and unit-tested.
    """

    def __init__(self, store: Any, on_done: Any = None) -> None:
        self._store = store
        # #852: fired once when a build reaches ``done`` (from ``_done``). The
        # reaper only writes the job-state row; the AgentService layer injects
        # this to release the build's pooled credential, drain the queue, and
        # emit the completion event + TFactory handoff — none of which the row
        # write does. Async ``(job_id) -> None``; None in tests / when unused.
        self._on_done = on_done

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

    async def dispatch(  # noqa: PLR0913 - all keyword-only dispatch coordinates
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        correlation_key: str | None = None,
        oauth_token: str | None = None,
        batch: Any = None,
        stop_after_planning: bool = False,
        parallel: bool | None = None,
        workers: int | None = None,
        force: bool = False,
        base_branch: str | None = None,
        mode: str | None = None,
    ) -> str:
        """Create the run.py Job and record its worker_ref. Returns the Job name.

        The caller has already reserved the durable slot (the job-state row is
        ``running`` with ``worker_ref={kind:subprocess}`` from ``admit``); we
        overwrite worker_ref with the k8s-job reference so the reconcile/reaper
        loops can find the Job. The Job itself flips the row to its terminal
        state when it finishes; we never block on it here.

        Before the Job is created we POPULATE the per-task worktree at the
        data-PVC subPath the Job co-mounts at ``/work`` (#671 build-worktree
        defect): the control plane and the Job pod share the same single-node
        data PVC, so the worktree + materialized spec we write here are exactly
        what the Job reads. Without this the Job saw an empty stub and run.py
        exited ``Spec '<id>' not found``. See ``populate_build_worktree``.

        ``oauth_token`` is the pooled Claude credential (#670) the CALLER checked
        out for this task, so concurrent Jobs draw DISTINCT tokens — it (plus the
        provider/runtime SDK env present on the control plane) is injected into
        the Job container env (#671 OAuth-env defect): a fresh Job pod inherits
        none of the control-plane env, so without it run.py started but died
        ``No OAuth token found``. See ``build_job_env``. Injected via the
        container ``env`` (never argv) so the token is not ps-visible.
        """
        # #671: create + populate the worktree (git worktree + spec) the SAME way
        # the in-pod path does, at the subPath the manifest co-mounts at /work,
        # BEFORE the Job exists. Done first so a population failure never leaves a
        # dangling Job pointed at an unpopulated /work.
        worktree_path = populate_build_worktree(project_path, spec_id)

        # RFC-0017 #190: when the producer is ON (AIFACTORY_PACK_WORKSPACE), pack
        # the populated worktree to object storage and hand the Job its
        # WORKSPACE_URI (multi-node handoff). OFF by default → None → the manifest
        # keeps today's single-node /work co-mount unchanged.
        workspace_uri = _maybe_pack_workspace(
            worktree_path, task_id=task_id, correlation_key=correlation_key
        )

        # #671 OAuth-env defect: mirror the in-pod build env into the Job (the
        # pooled token + the SDK passthrough). Goes into container env, NOT argv.
        extra_env = build_job_env(oauth_token)

        manifest = build_run_py_job_manifest(
            task_id=task_id,
            project_path=project_path,
            spec_id=spec_id,
            correlation_key=correlation_key,
            extra_env=extra_env,
            workspace_uri=workspace_uri,
            stop_after_planning=stop_after_planning,
            parallel=parallel,
            workers=workers,
            force=force,
            base_branch=base_branch,
            mode=mode,
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
            namespace,
            job_name,
            task_id,
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
                namespace,
                job_name,
                job_id,
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

                outcome = await self._job_outcome(batch, namespace, job_name)

                # #857: reconcile from the Job's OWN status. The Job cannot write
                # its job-state row (mark_terminal lives only in the control
                # plane; run.py has no job-state write), so waiting for one meant
                # every SUCCESSFUL build sat here until ttlSecondsAfterFinished
                # GC'd the Job and the branch below marked it failed. The kubelet
                # already told us the answer; take it.
                if outcome == "succeeded":
                    await self._done(job_id)
                    continue
                if outcome == "failed":
                    await self._fail(
                        job_id,
                        f"k8s Job {namespace}/{job_name} reported failed "
                        "(backoffLimit=0 — one attempt, no retry)",
                    )
                    reaped.append(job_id)
                    continue

                if outcome == "running":
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
                # writing a terminal state. Don't strand it. Post-#857 this is a
                # GENUINE anomaly (evicted / GC'd before any tick observed it),
                # not the everyday path it used to be.
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

    async def _done(self, job_id: str) -> None:
        """Mark a build done from the Job's own success (#857).

        The counterpart to :meth:`_fail`. Idempotent via the store, so observing
        the same succeeded Job on several 15s ticks before its TTL expires is a
        harmless no-op after the first.
        """
        try:
            await self._store.mark_terminal(job_id, "done")
            _log.info(
                "[build_backend] build %s done (k8s Job reported succeeded)", job_id
            )
        except Exception:  # noqa: BLE001 - reconcile must never crash the loop
            _log.exception("[build_backend] could not mark %s done", job_id)
            return
        # #852: fires once (the row leaves ``running`` above, so the reaper never
        # revisits it). Guarded — a completion-emit failure must not crash the
        # reconcile loop or re-strand the (already ``done``) build.
        if self._on_done is not None:
            try:
                await self._on_done(job_id)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "[build_backend] on_done hook raised for %s (ignored)", job_id
                )

    async def _fail(self, job_id: str, reason: str) -> None:
        """Mark a stranded build failed (idempotent via the store)."""
        try:
            await self._store.mark_terminal(job_id, "failed", error=reason)
            _log.warning("[build_backend] reaped stranded build %s: %s", job_id, reason)
        except Exception:  # noqa: BLE001 - reaper must never crash the loop
            _log.exception("[build_backend] could not reap %s", job_id)

    @staticmethod
    async def _job_outcome(batch: Any, namespace: str, job_name: str) -> str:
        """The Job's real state: ``succeeded`` | ``failed`` | ``running`` | ``gone``.

        #857: this used to be ``_job_exists`` and returned a bool, discarding the
        Job object it had just fetched — including ``.status.succeeded``. That was
        the whole bug: ``job_dispatch``'s contract says "the Job writes its own
        job-state row", but NOTHING in ``apps/backend`` can — ``mark_terminal``
        exists only in the control plane, and ``run.py`` has no job-state write at
        all. So no build ever wrote a terminal row; the reaper waited out
        ``ttlSecondsAfterFinished`` (300s), found the Job GC'd, and marked every
        SUCCESSFUL build failed -> human_review. The kubelet's verdict was in hand
        on every 15s tick and thrown away.

        ``.status.succeeded``/``.failed`` is authoritative here: ``backoffLimit:
        0`` means exactly one attempt, so the counts are unambiguous.

        A 404 means gone. Any other error is reported as ``running`` — fail safe,
        never reap a build we could not verify (unchanged from ``_job_exists``).
        """
        try:
            job = await batch.read_namespaced_job(job_name, namespace)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                return "gone"
            _log.warning(
                "[build_backend] could not verify Job %s/%s (%s) — assuming present",
                namespace,
                job_name,
                exc,
            )
            return "running"
        status = getattr(job, "status", None)
        if (getattr(status, "succeeded", None) or 0) >= 1:
            return "succeeded"
        if (getattr(status, "failed", None) or 0) >= 1:
            return "failed"
        return "running"


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
