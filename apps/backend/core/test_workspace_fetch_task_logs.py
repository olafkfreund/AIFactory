"""W1 (Factory #218): task_logs.json propagation on the packed path.

The build Job writes ``task_logs.json`` into its ephemeral ``/work`` spec dir;
the control plane never sees it, so the task list defaults to ``backlog``.
``maybe_push_task_logs`` uploads it (keyed by spec_id) and
``maybe_fetch_task_logs`` pulls it onto the data-PVC spec dir so the real status
is reported. Mirrors the token_usage.json (#190) propagation.
"""

from __future__ import annotations

import core.workspace_fetch as wf


class _FakeStore:
    """In-memory ArtifactStore stand-in keyed exactly like the real one."""

    _blobs: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        _FakeStore._blobs[key] = data
        return key

    def get_bytes(self, key: str) -> bytes:
        return _FakeStore._blobs[key]


def _patch_store(monkeypatch):
    _FakeStore._blobs = {}
    monkeypatch.setattr("core.artifact_store.ArtifactStore", _FakeStore)


def test_push_is_noop_off_packed_path(tmp_path, monkeypatch):
    monkeypatch.delenv(wf.WORKSPACE_URI_ENV, raising=False)
    (tmp_path / "task_logs.json").write_text("{}")
    assert wf.maybe_push_task_logs(tmp_path, "001-x") is False


def test_push_is_noop_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/x")
    _patch_store(monkeypatch)
    assert wf.maybe_push_task_logs(tmp_path, "001-x") is False


def test_fetch_is_noop_when_dest_exists(tmp_path, monkeypatch):
    _patch_store(monkeypatch)
    (tmp_path / "task_logs.json").write_text('{"phases": {}}')
    assert wf.maybe_fetch_task_logs(tmp_path, "001-x") is False


def test_fetch_is_noop_when_nothing_pushed(tmp_path, monkeypatch):
    _patch_store(monkeypatch)
    assert wf.maybe_fetch_task_logs(tmp_path, "001-x") is False


def test_push_then_fetch_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/x")
    _patch_store(monkeypatch)

    # Producer side: the Job's spec dir has task_logs.json.
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    payload = '{"phases": {"coding": {"status": "done"}}}'
    (job_dir / "task_logs.json").write_text(payload)
    assert wf.maybe_push_task_logs(job_dir, "001-feature") is True

    # Consumer side: a fresh control-plane spec dir with no task_logs.json.
    ctrl_dir = tmp_path / "ctrl"
    ctrl_dir.mkdir()
    assert wf.maybe_fetch_task_logs(ctrl_dir, "001-feature") is True
    assert (ctrl_dir / "task_logs.json").read_text() == payload


def test_keys_match_for_same_spec_id():
    # Producer and consumer derive the SAME key from spec_id alone.
    assert wf._task_logs_key("001-feature") == wf._task_logs_key("001-feature")
    assert wf._task_logs_key("001-a") != wf._task_logs_key("001-b")
