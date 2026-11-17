"""#2034: transcript upload and listing through the artifact store."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

import core.workspace_fetch as wf
import pytest
import task_logger
from task_logger import transcript


class _Cfg:
    bucket = "factory-artifacts"


class _FakeStore:
    blobs: ClassVar[dict[str, tuple[bytes, str | None]]] = {}
    config: _Cfg = _Cfg()

    def put_bytes(
        self, key: str, data: bytes, _ct: str | None = None, role: str | None = None
    ) -> str:
        _FakeStore.blobs[key] = (data, role)
        return key

    def list_artifacts(self, _ref: object, role_scoped: bool = False) -> list[str]:
        assert role_scoped
        return list(_FakeStore.blobs)


@pytest.fixture(autouse=True)
def _store(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeStore.blobs = {}
    monkeypatch.setattr("core.artifact_store.ArtifactStore", _FakeStore)
    # Other test modules leave a MagicMock task_logger in sys.modules; the
    # code under test imports it at call time, so pin the real one.
    monkeypatch.setitem(sys.modules, "task_logger", task_logger)
    transcript.reset()


def test_push_is_noop_without_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    transcript.start(tmp_path)
    transcript.record("tool_start", "Read", {})
    assert wf.maybe_push_transcripts("001-x") == 0
    assert transcript.written_files()  # local file kept


def test_push_uploads_with_evidence_role_and_removes_local(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "http://minio")
    transcript.start(tmp_path)
    transcript.record("tool_start", "Read", {"a": 1})
    local = transcript.written_files()[0]
    assert wf.maybe_push_transcripts("001-x") == 1
    (key, (data, role)) = next(iter(_FakeStore.blobs.items()))
    assert key == f"aifactory/_/001-x/evidence/transcripts/{local.name}"
    assert role == "evidence" and b"tool_start" in data
    assert not local.exists()


def test_push_key_is_project_qualified_by_job_id(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "http://minio")
    monkeypatch.setenv("JOB_ID", "proj-a:001-x")
    transcript.start(tmp_path)
    transcript.record("tool_start", "Read", {})
    assert wf.maybe_push_transcripts("001-x") == 1
    other = "aifactory/_/proj-b:001-x/evidence/transcripts/z.jsonl"
    _FakeStore.blobs[other] = (b"", None)
    uris = wf.transcript_uris("proj-a:001-x")
    assert len(uris) == 1 and "proj-a:001-x" in uris[0] and "proj-b" not in uris[0]


def test_push_nothing_recorded_uploads_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "http://minio")
    transcript.start(tmp_path)
    assert wf.maybe_push_transcripts("001-x") == 0


def test_uris_filtered_sorted_and_errors_swallowed(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "http://minio")
    base = "aifactory/_/001-x"
    for k in (
        f"{base}/evidence/transcripts/b.jsonl",
        f"{base}/evidence/transcripts/a.jsonl",
        f"{base}/evidence/other.json",
        f"{base}/build/task_logs.json",
    ):
        _FakeStore.blobs[k] = (b"", None)
    assert wf.transcript_uris("001-x") == [
        f"s3://factory-artifacts/{base}/evidence/transcripts/a.jsonl",
        f"s3://factory-artifacts/{base}/evidence/transcripts/b.jsonl",
    ]
    monkeypatch.setattr(
        _FakeStore,
        "list_artifacts",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")),
    )
    assert wf.transcript_uris("001-x") == []
    monkeypatch.delenv("S3_ENDPOINT")
    assert wf.transcript_uris("001-x") == []


def test_since_drops_earlier_runs_at_the_cutoff(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "http://minio")
    base = "aifactory/_/p:s/evidence/transcripts/"
    for stamp in ("20261117T100000_000000Z", "20261117T100001_000000Z"):
        _FakeStore.blobs[f"{base}{stamp}-0000001.jsonl"] = (b"", None)
    assert len(wf.transcript_uris("p:s")) == 2
    # Names equal to or after the cutoff stay; the one just before goes.
    kept = wf.transcript_uris("p:s", "20261117T100001_000000Z")
    assert [u.rsplit("/", 1)[-1][:23] for u in kept] == ["20261117T100001_000000Z"]
    assert wf.transcript_uris("p:s", "20261117T100002_000000Z") == []


_CHILD = """
import sys, time
import core.workspace_fetch as wf
def fake_push(spec_id):
    print("uploaded", spec_id, flush=True)
    time.sleep(float(sys.argv[1]))
wf.maybe_push_transcripts = fake_push
wf.install_sigterm_upload("spec-1")
print("ready", flush=True)
time.sleep(60)
"""


def _term_child(upload_seconds: float, bound: str) -> tuple[int, str, float]:
    backend = str(Path(wf.__file__).resolve().parents[1])
    env = {
        **os.environ,
        "PYTHONPATH": backend,
        "AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS": bound,
    }
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-only
        [sys.executable, "-c", _CHILD, str(upload_seconds)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    start = time.monotonic()
    proc.terminate()
    out = proc.stdout.read()
    code = proc.wait(timeout=20)
    return code, out, time.monotonic() - start


def test_sigterm_uploads_then_exits_143():
    code, out, _ = _term_child(0, "5")
    assert code == 143 and "uploaded spec-1" in out


def test_sigterm_with_a_stalled_upload_exits_within_the_bound():
    code, out, elapsed = _term_child(30, "1")
    assert code == 143 and "uploaded spec-1" in out
    assert elapsed < 10
