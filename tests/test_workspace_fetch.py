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

    dest = tmp_path / "work"
    assert maybe_unpack_workspace(dest) is True
    assert (dest / "main.py").read_text() == "print('hi')\n"
    assert (dest / "pkg" / "mod.py").read_text() == "X = 1\n"


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
