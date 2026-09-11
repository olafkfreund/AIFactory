"""KubeJobBackend (#68): manifest builder + gate_runner kubejob selection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.gate_runner import _default_runner, _select_runner  # noqa: E402
from core.factory_sandbox import RunResult  # noqa: E402
from core.kube_sandbox import _pvc_subpath, build_job_manifest  # noqa: E402


def test_manifest_is_one_shot_gc_hardened():
    m = build_job_manifest(
        "fsbx-abc", "ghcr.io/x/rust:1.90", ["cargo build", "cargo test"]
    )
    assert m["kind"] == "Job" and m["metadata"]["name"] == "fsbx-abc"
    spec = m["spec"]
    assert spec["backoffLimit"] == 0  # one shot, no retries
    assert spec["ttlSecondsAfterFinished"] == 120  # auto-GC
    t = spec["template"]["spec"]
    assert t["restartPolicy"] == "Never"
    assert t["automountServiceAccountToken"] is False  # gate needs no k8s API
    assert t["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    c = t["containers"][0]
    assert c["image"] == "ghcr.io/x/rust:1.90"
    assert c["command"] == ["bash", "-c", "cargo build && cargo test"]
    assert c["resources"]["limits"]["memory"] == "2Gi"


def test_manifest_pod_hardening_and_task_label():
    # #812 (Factory#274 compensating controls), corrected by #840: pinned
    # securityContext on every gate Job pod/container, and the factory.io/kind=task
    # pod label that puts the pod under the chart's per-task NetworkPolicy.
    m = build_job_manifest(
        "fsbx-abc", "img", ["nix --version"], nix_store_pvc="aifactory-nix-store"
    )
    tpl = m["spec"]["template"]
    assert tpl["metadata"]["labels"]["factory.io/kind"] == "task"
    t = tpl["spec"]
    assert t["securityContext"] == {"seccompProfile": {"type": "RuntimeDefault"}}
    hardened = {
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "capabilities": {
            "drop": ["ALL"],
            "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"],
        },
    }
    assert t["containers"][0]["securityContext"] == hardened
    assert t["initContainers"][0]["securityContext"] == hardened


def test_gate_keeps_caps_nix_local_builds_need():
    """#840: the image ships `build-users-group = nixbld`, so a local build makes
    nix setuid to a build user and reap it. Dropping SETUID/SETGID/KILL kills
    `nix develop` with "setting uid: Operation not permitted" the moment a
    derivation cannot be substituted from the binary cache — proven on the live
    cluster, where adding exactly these three turned a failing gate Job into a
    succeeding one that built python3-*-env.drv and nix-shell-env.drv locally.

    Load-bearing since #830/#253 removed the warm store: a cold /nix substitutes
    most paths but still builds the shell env.
    """
    t = build_job_manifest("fsbx-abc", "img", ["nix --version"])["spec"]["template"][
        "spec"
    ]
    add = set(t["containers"][0]["securityContext"]["capabilities"]["add"])
    assert {"SETUID", "SETGID", "KILL"} <= add, add


def test_gate_pod_never_pins_runasnonroot():
    """#840 regression: the gate image (factory-runner-nix) is USER 0:0 because
    nix builds run as root and nix must write /nix/var. #812 set runAsNonRoot
    here on the premise that "task images declare a non-root USER" — true of the
    BUILD image, false of this one — and the kubelet then refused the container
    outright ("container has runAsNonRoot and image will run as root",
    CreateContainerConfigError), so every gate Job died before running a single
    command. TFactory#651 declined to set it for exactly this reason.

    Do not "fix" a recurrence with runAsUser: the image's /nix is root-owned.
    """
    t = build_job_manifest("fsbx-abc", "img", ["nix --version"])["spec"]["template"][
        "spec"
    ]
    assert "runAsNonRoot" not in t["securityContext"], t["securityContext"]
    assert "runAsUser" not in t["securityContext"], t["securityContext"]
    # The rest of the #812 hardening must survive this correction.
    assert t["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    sc = t["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]


def test_manifest_no_repo_mount_by_default():
    # Toolchain-only Job (back-compat): no PVC volume, no workingDir.
    m = build_job_manifest("fsbx-abc", "img", ["go version"])
    t = m["spec"]["template"]["spec"]
    assert "volumes" not in t
    assert "volumeMounts" not in t["containers"][0]
    assert "workingDir" not in t["containers"][0]


def test_manifest_co_mounts_worktree_via_pvc_subpath():
    m = build_job_manifest(
        "fsbx-abc",
        "img",
        ["go build ./..."],
        repo_pvc="aifactory-data",
        repo_subpath="workspaces/olafkfreund-hello-go/.aifactory/worktrees/tasks/hello-go",
    )
    t = m["spec"]["template"]["spec"]
    assert t["volumes"] == [
        {
            "name": "repo",
            "persistentVolumeClaim": {"claimName": "aifactory-data", "readOnly": False},
        }
    ]
    c = t["containers"][0]
    assert c["workingDir"] == "/work"
    vm = c["volumeMounts"][0]
    assert vm["name"] == "repo" and vm["mountPath"] == "/work"
    assert vm["subPath"].endswith("worktrees/tasks/hello-go")
    assert vm["readOnly"] is False


def test_manifest_no_nix_store_mount_by_default():
    # RFC-0016 #197: cold behavior unchanged when no warm-store PVC named.
    m = build_job_manifest("fsbx-abc", "img", ["nix --version"])
    t = m["spec"]["template"]["spec"]
    assert "initContainers" not in t
    assert "volumes" not in t


def test_manifest_mounts_warm_nix_store_with_seed_init():
    # RFC-0016 #197: the whole /nix tree is served from the warm-store PVC, and a
    # seed initContainer populates it from the image on first use.
    m = build_job_manifest(
        "fsbx-abc",
        "ghcr.io/olafkfreund/factory-runner-nix:latest",
        ["nix develop path:/work#default -c go build ./..."],
        repo_pvc="aifactory-data",
        repo_subpath="ws/proj/.aifactory/worktrees/tasks/t",
        nix_store_pvc="aifactory-nix-store",
    )
    t = m["spec"]["template"]["spec"]
    # store volume present alongside the repo co-mount
    vols = {v["name"]: v for v in t["volumes"]}
    assert (
        vols["nix-store"]["persistentVolumeClaim"]["claimName"] == "aifactory-nix-store"
    )
    assert "repo" in vols
    # gate container mounts the warm store at /nix
    mounts = {vm["name"]: vm for vm in t["containers"][0]["volumeMounts"]}
    assert mounts["nix-store"]["mountPath"] == "/nix"
    # seed init container copies the image's /nix into the empty PVC at /warm
    init = t["initContainers"][0]
    assert init["name"] == "seed-nix-store"
    assert init["image"] == "ghcr.io/olafkfreund/factory-runner-nix:latest"
    assert init["volumeMounts"][0]["mountPath"] == "/warm"
    assert "/warm/store" in init["command"][-1]


def test_seed_init_container_seeds_atomically():
    # #1545: check-then-copy let two concurrent Jobs both see a missing store,
    # or one see a partially-copied tree. The seed must build the tree in a
    # scratch dir on the SAME filesystem and `mv` it into place, treating an
    # already-present target as success (no re-copy, no clobber).
    m = build_job_manifest(
        "fsbx-abc", "img", ["nix --version"], nix_store_pvc="aifactory-nix-store"
    )
    script = m["spec"]["template"]["spec"]["initContainers"][0]["command"][-1]
    # Still checks for an already-populated store before doing any work.
    assert "[ -e /warm/store ]" in script
    # Builds the tree in a scratch dir under /warm (same filesystem as the
    # target, so the rename below is atomic) rather than copying straight
    # onto /warm/store.
    assert "tmp=/warm/.seed-$$" in script
    assert "cp -a /nix/." in script
    # The rename into place uses `mv -n` (no-clobber): a Job that loses the
    # race to a concurrent seed discards its own copy instead of overwriting
    # (or partially overwriting) the winner's.
    assert 'mv -n "$tmp/store" /warm/store' in script
    assert script.count("mv -n") >= 2  # the store rename, plus its siblings
    # Scratch dir is always cleaned up, on both the winning and losing paths.
    assert 'rm -rf "$tmp"' in script


def test_pvc_subpath_strips_data_root():
    root = "/home/nonroot/.aifactory"
    wt = root + "/workspaces/proj/.aifactory/worktrees/tasks/spec-x"
    assert _pvc_subpath(wt, root) == "workspaces/proj/.aifactory/worktrees/tasks/spec-x"
    assert _pvc_subpath(root, root) == ""  # PVC root itself
    assert _pvc_subpath("/tmp/elsewhere", root) is None  # outside PVC -> no mount
    assert _pvc_subpath(None, root) is None  # unset -> no mount


def test_select_runner_kubejob_backend(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/x/go:1.25")
    monkeypatch.setenv("AIFACTORY_SANDBOX_BACKEND", "kubejob")

    import core.kube_sandbox as ks

    calls = {}

    class FakeKubeSandbox:
        def __init__(self, image, **kw):
            calls["image"] = image

        def run(self, commands, **kw):
            calls["commands"] = commands
            return RunResult(True, 0, "go1.25", [])

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeKubeSandbox)

    runner = _select_runner()
    assert runner is not _default_runner
    code, out = runner(["go", "version"], Path("/work"))
    assert code == 0 and out == "go1.25"
    assert calls["image"] == "ghcr.io/x/go:1.25"
    assert calls["commands"] == ["go version"]  # argv shlex-joined


def test_select_runner_defaults_to_docker_backend(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    monkeypatch.delenv("AIFACTORY_SANDBOX_BACKEND", raising=False)
    # docker backend selected (not the host runner, not kube)
    assert _select_runner() is not _default_runner


def test_kube_backend_error_is_gate_failure(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    monkeypatch.setenv("AIFACTORY_SANDBOX_BACKEND", "kubejob")
    import core.kube_sandbox as ks

    class Boom:
        def __init__(self, *a, **k): ...
        def run(self, *a, **k):
            raise RuntimeError("api down")

    monkeypatch.setattr(ks, "KubeJobSandbox", Boom)
    code, out = _select_runner()(["x"], Path("/w"))
    assert code == 1 and "kube-sandbox error" in out


# --- RFC-0005: real container exit code (not the Job's synthetic 0/1) ---
from types import SimpleNamespace  # noqa: E402

from core.kube_sandbox import _exit_code_from_pod  # noqa: E402


def _pod(exit_code):
    term = SimpleNamespace(exit_code=exit_code) if exit_code is not None else None
    state = SimpleNamespace(terminated=term)
    cs = SimpleNamespace(state=state)
    return SimpleNamespace(status=SimpleNamespace(container_statuses=[cs]))


def test_exit_code_reads_real_container_code():
    # Job flag says succeeded, but the container actually exited 2 → report 2.
    assert _exit_code_from_pod(_pod(2), job_succeeded=True) == (False, 2)
    # Real zero exit → succeeded True.
    assert _exit_code_from_pod(_pod(0), job_succeeded=False) == (True, 0)


def test_exit_code_falls_back_to_job_flag_when_no_terminated_state():
    # No terminated state available → synthetic fallback from the Job flag.
    assert _exit_code_from_pod(_pod(None), job_succeeded=True) == (True, 0)
    assert _exit_code_from_pod(
        SimpleNamespace(status=SimpleNamespace(container_statuses=None)),
        job_succeeded=False,
    ) == (False, 1)


def test_job_failure_reason_names_the_deadline():
    """A Job killed by activeDeadlineSeconds never ran the command to completion.

    It has no terminated container state, so the exit code falls back to a
    synthetic 1 — the same value a genuinely failing test produces. Without the
    reason, "the toolchain download did not finish" is indistinguishable from
    "your tests failed" (AIFactory#1491 family).
    """
    from types import SimpleNamespace

    from core.kube_sandbox import _job_failure_reason

    killed = SimpleNamespace(
        conditions=[SimpleNamespace(type="Failed", reason="DeadlineExceeded")]
    )
    assert _job_failure_reason(killed) == "DeadlineExceeded"

    # A Failed condition with no reason still reports something usable.
    assert (
        _job_failure_reason(
            SimpleNamespace(conditions=[SimpleNamespace(type="Failed", reason=None)])
        )
        == "Failed"
    )
    # A healthy or unknown status must not invent a reason.
    assert _job_failure_reason(SimpleNamespace(conditions=[])) == ""
    assert _job_failure_reason(SimpleNamespace(conditions=None)) == ""
    assert (
        _job_failure_reason(
            SimpleNamespace(conditions=[SimpleNamespace(type="Complete", reason="x")])
        )
        == ""
    )


class _FakeJobStatus:
    def __init__(self, *, succeeded=None, failed=None, conditions=None):
        self.succeeded = succeeded
        self.failed = failed
        self.conditions = conditions or []


class _FakeJob:
    def __init__(self, status):
        self.status = status


class _FakeBatchApi:
    """Feeds one `.status` per `read_namespaced_job` call, in order."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._i = 0

    async def create_namespaced_job(self, namespace, manifest):
        return None

    async def read_namespaced_job(self, name, namespace):
        status = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        return _FakeJob(status)

    async def delete_namespaced_job(self, name, namespace, propagation_policy=None):
        return None


class _FakeCoreApi:
    async def list_namespaced_pod(self, namespace, label_selector=None):
        from types import SimpleNamespace

        return SimpleNamespace(items=[])  # no pod -> loop flags decide the result


async def _run_seeded(monkeypatch, statuses, *, timeout=3):
    """Drive `KubeJobSandbox._run_async` with a scripted sequence of Job
    statuses, one per `read_namespaced_job` call, and a stubbed k8s client."""
    import kubernetes_asyncio.client as k8s_client
    import kubernetes_asyncio.config as k8s_config
    from core.kube_sandbox import KubeJobSandbox

    async def _noop(*_a, **_k):
        return None

    class _FakeApiClient:
        async def close(self):
            return None

    fake_batch = _FakeBatchApi(statuses)
    fake_core = _FakeCoreApi()

    monkeypatch.setattr(
        k8s_config,
        "load_incluster_config",
        lambda: (_ for _ in ()).throw(Exception("no in-cluster config in tests")),
    )
    monkeypatch.setattr(k8s_config, "load_kube_config", _noop)
    monkeypatch.setattr(k8s_client, "ApiClient", lambda: _FakeApiClient())
    monkeypatch.setattr(k8s_client, "BatchV1Api", lambda api: fake_batch)
    monkeypatch.setattr(k8s_client, "CoreV1Api", lambda api: fake_core)

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _noop)

    sandbox = KubeJobSandbox(image="img", namespace="ns")
    return await sandbox._run_async(["pytest", "-q"], timeout=timeout)


async def test_a_job_that_fails_during_the_final_sleep_is_still_caught(monkeypatch):
    """#1545: `timeout // 3 == 1` gives the loop exactly ONE iteration. That
    read shows the Job still running, so only a read taken AFTER the loop
    (once the final `asyncio.sleep` has elapsed) can see the Job transition to
    Failed/DeadlineExceeded. Without the post-loop read, the `[job ...]`
    reason silently disappears from the output."""
    running = _FakeJobStatus(succeeded=None, failed=None)
    failed = _FakeJobStatus(
        succeeded=None,
        failed=1,
        conditions=[SimpleNamespace(type="Failed", reason="DeadlineExceeded")],
    )
    result = await _run_seeded(monkeypatch, [running, failed], timeout=3)

    assert result.ok is False
    assert "[job DeadlineExceeded]" in result.output


async def test_a_job_that_succeeds_before_the_deadline_is_unaffected(monkeypatch):
    succeeded = _FakeJobStatus(succeeded=1, failed=None)
    result = await _run_seeded(monkeypatch, [succeeded], timeout=3)

    assert result.ok is True
    assert "[job" not in result.output
