"""The Job's implementation_plan.json must reach the control plane (#852).

The packed Job records each subtask "completed" in the plan inside its ephemeral
/work. The control plane counts completed subtasks from the data-PVC spec dir,
which the packed path never populates, so it saw 0 and applied the #287 guard —
"a clean exit with no successful subtask is emitted as FAILED". Every successful
packed build therefore escalated to human_review + errors, blocking the TFactory
handoff (Factory#253 "output propagation").

Proven live on aifactory-demo#320: correct patch, branch pushed, and
  control-plane spec dir: requirements.json  spec.md  task_metadata.json
  pushed branch:          implementation_plan.json (all subtasks "completed")
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

import core.workspace_fetch as wf  # noqa: E402

_PLAN_PENDING = {
    "phases": [{"phase": 1, "subtasks": [{"id": "1.1", "status": "pending"}]}]
}
_PLAN_DONE = {
    "phases": [{"phase": 1, "subtasks": [{"id": "1.1", "status": "completed"}]}]
}


class _FakeStore:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = blobs or {}

    def put_bytes(
        self, key: str, data: bytes, _content_type: str | None = None, **_: object
    ) -> str:
        # **_ swallows the `role=` kwarg (and any future ones) the real
        # ArtifactStore.put_bytes accepts, so callers like maybe_push_plan work.
        self.blobs[key] = data
        return f"s3://factory-artifacts/{key}"

    def get_bytes(self, key: str) -> bytes:
        return self.blobs[key]  # KeyError -> caller treats as "nothing pushed"


def _patch_store(monkeypatch, store: _FakeStore) -> None:
    monkeypatch.setattr("core.artifact_store.ArtifactStore", lambda *_a, **_k: store)


def test_push_is_a_noop_off_the_packed_path(monkeypatch, tmp_path):
    """Co-mount path: the spec dir is already on the data PVC — nothing to push."""
    monkeypatch.delenv(wf.WORKSPACE_URI_ENV, raising=False)
    (tmp_path / "implementation_plan.json").write_text(json.dumps(_PLAN_DONE))
    assert wf.maybe_push_plan(tmp_path, "001-x") is False


def test_push_then_fetch_carries_completed_subtasks(monkeypatch, tmp_path):
    """The whole point: the control plane must end up with the JOB's plan."""
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    monkeypatch.setenv(
        wf.WORKSPACE_URI_ENV, "s3://factory-artifacts/x/workspace.tar.gz"
    )

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "implementation_plan.json").write_text(json.dumps(_PLAN_DONE))
    assert wf.maybe_push_plan(job_dir, "001-x") is True

    # The control plane holds the PRE-DISPATCH original: every subtask pending.
    ctrl_dir = tmp_path / "ctrl"
    ctrl_dir.mkdir()
    (ctrl_dir / "implementation_plan.json").write_text(json.dumps(_PLAN_PENDING))

    assert wf.maybe_fetch_plan(ctrl_dir, "001-x") is True
    got = json.loads((ctrl_dir / "implementation_plan.json").read_text())
    statuses = [s["status"] for ph in got["phases"] for s in ph["subtasks"]]
    assert statuses == ["completed"], got


def test_fetch_overwrites_the_stale_control_plane_copy(monkeypatch, tmp_path):
    """Regression guard for the whole bug.

    The sibling fetchers bail when the file already exists, because the control
    plane never has their artifact. The plan is different: the control plane wrote
    the original before dispatch. A bail-if-present guard here would keep the
    stale all-pending copy and reproduce #852 exactly — build_succeeded=False on a
    green build.
    """
    store = _FakeStore({wf._plan_key("001-x"): json.dumps(_PLAN_DONE).encode()})
    _patch_store(monkeypatch, store)

    (tmp_path / "implementation_plan.json").write_text(json.dumps(_PLAN_PENDING))
    assert wf.maybe_fetch_plan(tmp_path, "001-x") is True
    got = json.loads((tmp_path / "implementation_plan.json").read_text())
    assert got["phases"][0]["subtasks"][0]["status"] == "completed", (
        "fetch must overwrite the stale pre-dispatch plan, else the control plane "
        "counts 0 completed subtasks and escalates every green build (#852)"
    )


def test_fetch_is_best_effort_when_nothing_was_pushed(monkeypatch, tmp_path):
    """No pushed plan (co-mount path / store down) must never raise."""
    _patch_store(monkeypatch, _FakeStore())
    assert wf.maybe_fetch_plan(tmp_path, "001-x") is False


def test_push_and_fetch_agree_on_the_key():
    """Both sides derive the key from spec_id alone — no URI threading."""
    assert wf._plan_key("001-x").endswith("/implementation_plan.json")
    assert "001-x" in wf._plan_key("001-x")
