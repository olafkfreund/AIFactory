#!/usr/bin/env python3
"""factory-sandbox — shared ephemeral-container primitive (RFC-0005, epic #61 Phase 1).

Extracted from TFactory's hardened `tools/runners/docker_runner.py` so AIFactory
(build/verify) and the RFC-0006 verification runner (#74) can launch a disposable,
isolated container per task instead of running in one long-lived pod. SDK-free
(argv + subprocess) and runtime-agnostic: prefers `podman` (rootless) and falls
back to `docker`.

Hardening mirrors TFactory's posture: `--rm`, `--network none` (opt-in
otherwise), CPU/memory/PID caps, read-only rootfs + a small writable `/tmp`
tmpfs, the work dir mounted read-only at /work by default (rw opt-in for the
coder), and an ephemeral rw `/scratch` as the cwd.

Provides `container_backend(image, workdir)` conforming to the #74 runner
interface `(level, commands) -> (ok, output)`, so the full stack composes:
profiles (#73) -> runner (#74) -> THIS sandbox -> gate (#72).

Run directly for the self-tests: `python3 scripts/factory_sandbox.py`.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass


class SandboxError(RuntimeError):
    pass


def detect_runtime() -> str:
    for b in ("podman", "docker"):
        if shutil.which(b):
            return b
    raise SandboxError("no container runtime found (need podman or docker)")


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    output: str
    argv: list[str]


class FactorySandbox:
    REPO_MOUNT = "/work"
    SCRATCH_MOUNT = "/scratch"

    def __init__(self, image: str, *, binary: str | None = None,
                 network: str = "none", cpus: float = 2, memory: str = "2g",
                 pids_limit: int = 512, read_only_rootfs: bool = True,
                 repo_rw: bool = False):
        self.image = image
        self.binary = binary or detect_runtime()
        self.network = network
        self.cpus, self.memory, self.pids_limit = cpus, memory, pids_limit
        self.read_only_rootfs = read_only_rootfs
        self.repo_rw = repo_rw

    def _argv(self, command: str, workdir: str | None, scratch: str,
              network: str | None) -> list[str]:
        argv = [self.binary, "run", "--rm",
                "--network", network or self.network,
                "--cpus", str(self.cpus),
                "--memory", str(self.memory),
                "--pids-limit", str(self.pids_limit)]
        if workdir:
            mode = "rw" if self.repo_rw else "ro"
            argv += ["-v", f"{workdir}:{self.REPO_MOUNT}:{mode}"]
        argv += ["-v", f"{scratch}:{self.SCRATCH_MOUNT}:rw",
                 "-w", (self.REPO_MOUNT if workdir else self.SCRATCH_MOUNT)]
        if self.read_only_rootfs:
            argv += ["--read-only", "--tmpfs", "/tmp:rw,size=64m"]
        argv += [self.image, "bash", "-c", command]
        return argv

    def run(self, commands: list[str], *, workdir: str | None = None,
            network: str | None = None, timeout: int = 600,
            dry_run: bool = False) -> RunResult:
        command = " && ".join(commands)
        with tempfile.TemporaryDirectory(prefix="factory-scratch-") as scratch:
            argv = self._argv(command, workdir, scratch, network)
            if dry_run:
                return RunResult(True, 0, "(dry-run)", argv)
            try:
                p = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=timeout, check=False)
            except FileNotFoundError as e:
                raise SandboxError(f"runtime {self.binary!r} not executable: {e}")
            return RunResult(p.returncode == 0, p.returncode,
                             (p.stdout + p.stderr).strip(), argv)


def container_backend(image: str, workdir: str | None = None, **kw):
    """#74-runner backend: (level, commands) -> (ok, output), each in a fresh container."""
    sb = FactorySandbox(image, **kw)
    def run(level: str, commands: list[str]):
        res = sb.run(commands, workdir=workdir)
        return res.ok, res.output
    return run


# --------------------------------------------------------------------------- #
def _test() -> None:
    # argv hardening (dry-run, no runtime needed): mirrors TFactory's posture.
    sb = FactorySandbox("img:latest", binary="podman")
    r = sb.run(["echo hi", "echo bye"], workdir="/repo", dry_run=True)
    a = " ".join(r.argv)
    assert r.argv[:3] == ["podman", "run", "--rm"], r.argv
    assert "--network none" in a and "--cpus 2" in a and "--pids-limit 512" in a, a
    assert "--read-only" in a and "/tmp:rw,size=64m" in a, a
    assert "/repo:/work:ro" in a, a            # repo ro by default
    assert a.endswith("img:latest bash -c echo hi && echo bye"), a

    # repo_rw flips the mount to rw (coder build path).
    a2 = " ".join(FactorySandbox("img", binary="docker", repo_rw=True)
                  .run(["x"], workdir="/repo", dry_run=True).argv)
    assert "/repo:/work:rw" in a2 and a2.split()[0] == "docker", a2

    # restricted network opt-in.
    a3 = " ".join(sb.run(["x"], network="bridge", dry_run=True).argv)
    assert "--network bridge" in a3, a3

    print("factory_sandbox self-tests: passed")


if __name__ == "__main__":
    _test()
