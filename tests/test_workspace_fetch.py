"""Unit tests for the RFC-0017 Stage E (#190) workspace consumer.

The Job entrypoint (``cli/main.py``) calls ``core.workspace_fetch.maybe_unpack_workspace``
before it resolves the spec: when a packed-workspace Job sets ``WORKSPACE_URI`` it
reconstitutes ``/work`` from object storage; otherwise (the single-node co-mount
path, and every non-Job run) it is a no-op.

Both behaviours are pinned here through the artifact-store module's in-memory fake
S3, so they run on every PR without Docker, S3, or MinIO.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core import artifact_store as a_s  # noqa: E402
from core import workspace_fetch as wf  # noqa: E402
from core.workspace_fetch import maybe_unpack_workspace  # noqa: E402


def test_no_uri_is_noop(monkeypatch, tmp_path) -> None:
    # No WORKSPACE_URI → the co-mount path; the function must not touch the dest.
    monkeypatch.delenv("WORKSPACE_URI", raising=False)
    dest = tmp_path / "work"
    assert maybe_unpack_workspace(dest) is False
    assert not dest.exists()


def test_blank_uri_is_noop(monkeypatch, tmp_path) -> None:
    # A present-but-blank value (e.g. an unset templated env) is treated as absent.
    monkeypatch.setenv("WORKSPACE_URI", "   ")
    dest = tmp_path / "work"
    assert maybe_unpack_workspace(dest) is False
    assert not dest.exists()


def test_unpack_round_trip(monkeypatch, tmp_path) -> None:
    # Build a persistent fake store, pack a worktree into it, then assert the
    # consumer reconstitutes that exact tree at the dest from WORKSPACE_URI.
    store = a_s._fake_store()  # built BEFORE patching → no _fake_store recursion
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")
    (src / "pkg" / "mod.py").write_text("X = 1\n")
    ref = a_s.ArtifactRef(
        service="aifactory",
        job_id="proj-1:042-go",
        role="workspace",
        correlation_key=482,
    )
    uri = a_s.pack_workspace(store, ref, src)

    # The consumer constructs ArtifactStore() internally — route it to our store.
    monkeypatch.setattr(a_s, "ArtifactStore", lambda *a, **k: store)
    monkeypatch.setenv("WORKSPACE_URI", uri)
    # Stub the git safe.directory call so the test never writes the runner's
    # global gitconfig (asserted on its own in test_unpack_marks_git_safe_directory).
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: None)

    dest = tmp_path / "work"
    assert maybe_unpack_workspace(dest) is True
    assert (dest / "main.py").read_text() == "print('hi')\n"
    assert (dest / "pkg" / "mod.py").read_text() == "X = 1\n"


def test_unpack_marks_git_safe_directory(monkeypatch, tmp_path) -> None:
    # After a successful unpack the consumer must mark the dest git-safe: the
    # emptyDir /work mount root is root-owned while the build runs nonroot, so
    # git would otherwise refuse the repo with "dubious ownership" (#190).
    store = a_s._fake_store()
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("x\n")
    ref = a_s.ArtifactRef(
        service="aifactory", job_id="j", role="workspace", correlation_key=1
    )
    uri = a_s.pack_workspace(store, ref, src)
    monkeypatch.setattr(a_s, "ArtifactStore", lambda *a, **k: store)
    monkeypatch.setenv("WORKSPACE_URI", uri)

    calls: list[list[str]] = []
    monkeypatch.setattr(wf.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    dest = tmp_path / "work"
    assert maybe_unpack_workspace(dest) is True
    safe = [c for c in calls if "safe.directory" in c]
    assert safe, "expected git safe.directory to be configured after unpack"
    assert str(dest) in safe[0]


def test_fetch_error_propagates(monkeypatch, tmp_path) -> None:
    # Fail-LOUD: once on the URI path there is no co-mount fallback, so a fetch
    # error must raise (not silently build on an empty /work).
    def _boom(*_a, **_k):
        raise RuntimeError("object store unreachable")

    monkeypatch.setattr(a_s, "unpack_workspace", _boom)
    monkeypatch.setattr(a_s, "ArtifactStore", lambda *a, **k: object())
    monkeypatch.setenv(
        "WORKSPACE_URI", "s3://factory-artifacts/aifactory/482/j/workspace"
    )

    dest = tmp_path / "work"
    try:
        maybe_unpack_workspace(dest)
    except RuntimeError as exc:
        assert "object store unreachable" in str(exc)
    else:
        raise AssertionError("expected the fetch error to propagate")


# ---------------------------------------------------------------------------
# Producer push-back: persist the build branch on the packed (ephemeral /work)
# path so the control-plane handoff/PR push doesn't degrade to `main` (#190).
# ---------------------------------------------------------------------------


def test_push_no_uri_is_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("WORKSPACE_URI", raising=False)
    assert wf.maybe_push_workspace_branch(tmp_path, "042-x") is False


def test_push_no_worktree_is_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/key")
    # No .aifactory/worktrees/tasks/042-x dir → nothing to push.
    assert wf.maybe_push_workspace_branch(tmp_path, "042-x") is False


def test_push_branch_pushes_to_origin(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_faketoken")  # noqa: S105 - test placeholder
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x"
    wt.mkdir(parents=True)
    calls = []

    class _R:
        def __init__(self, stdout="", rc=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = rc

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return _R(stdout="aifactory/042-x")
        if args[:3] == ["git", "remote", "get-url"]:
            return _R(stdout="https://github.com/o/r.git")
        return _R(rc=0)  # push

    monkeypatch.setattr(wf.subprocess, "run", fake_run)
    assert wf.maybe_push_workspace_branch(tmp_path, "042-x") is True
    push = [c for c in calls if c[:2] == ["git", "push"]][0]
    # token injected into the push URL; pushes HEAD:<branch>
    assert push[2] == "https://x-access-token:ghs_faketoken@github.com/o/r.git"
    assert push[3] == "HEAD:aifactory/042-x"


# ---------------------------------------------------------------------------
# Usage propagation: token_usage.json from the packed Job's /work back to the
# control plane via object storage, so CFactory shows token usage (#190).
# ---------------------------------------------------------------------------


def test_push_usage_no_uri_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSPACE_URI", raising=False)
    (tmp_path / "token_usage.json").write_text("{}")
    assert wf.maybe_push_usage(tmp_path, "042-x") is False


def test_push_usage_no_file_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_URI", "s3://b/k")
    assert wf.maybe_push_usage(tmp_path, "042-x") is False


def test_usage_round_trip(monkeypatch, tmp_path):
    store = a_s._fake_store()
    monkeypatch.setattr(a_s, "ArtifactStore", lambda *a, **k: store)
    # producer: Job pushes token_usage.json
    monkeypatch.setenv("WORKSPACE_URI", "s3://b/k")
    job_spec = tmp_path / "job"
    job_spec.mkdir()
    (job_spec / "token_usage.json").write_text('{"total_tokens": 123}')
    assert wf.maybe_push_usage(job_spec, "042-x") is True
    # consumer: control-plane spec dir is empty -> fetch reconstitutes the file
    cp_spec = tmp_path / "cp"
    cp_spec.mkdir()
    assert wf.maybe_fetch_usage(cp_spec, "042-x") is True
    import json as _json

    assert _json.loads((cp_spec / "token_usage.json").read_text())["total_tokens"] == 123


def test_fetch_usage_noop_when_present(monkeypatch, tmp_path):
    (tmp_path / "token_usage.json").write_text("{}")
    # already present -> no fetch attempted (would not even need the store)
    assert wf.maybe_fetch_usage(tmp_path, "042-x") is False
