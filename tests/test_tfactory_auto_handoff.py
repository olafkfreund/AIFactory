"""#496: opt-in auto-handover of a finished task to TFactory for testing.

`maybe_auto_handoff_tfactory` fires only when task_metadata has
`auto_handover_tfactory` set; it builds the handoff payload from the spec's
requirements + meta and calls send_handoff. Best-effort; never raises.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from pfactory import tfactory_client as tc  # noqa: E402


def _spec(tmp: Path, opt_in: bool) -> Path:
    meta = {"auto_handover_tfactory": True} if opt_in else {}
    (tmp / "task_metadata.json").write_text(json.dumps(meta))
    (tmp / "requirements.json").write_text(
        json.dumps({"title": "t", "description": "d"})
    )
    return tmp


def test_wants_auto_handoff(tmp_path):
    assert tc.wants_auto_handoff(_spec(tmp_path, True)) is True
    (tmp_path / "task_metadata.json").write_text("{}")
    assert tc.wants_auto_handoff(tmp_path) is False
    assert tc.wants_auto_handoff(tmp_path / "does-not-exist") is False


def test_not_requested_is_noop(tmp_path):
    _spec(tmp_path, False)
    result = asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "001-x"))
    assert result == {"sent": False, "reason": "not_requested"}


def test_opted_in_sends_and_records(tmp_path, monkeypatch):
    _spec(tmp_path, True)
    captured = {}

    async def fake_send(payload, **kwargs):
        captured["payload"] = payload
        return {"sent": True, "reason": None, "status": 200}

    monkeypatch.setattr(tc, "send_handoff", fake_send)

    result = asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "001-x"))
    assert result["sent"] is True
    assert captured["payload"]["spec_id"] == "001-x"
    # #517: the handoff now uses TFactory's self-contained ingest contract
    # ({project_id, spec_id, spec_text}), not the legacy rich payload.
    assert "project_id" in captured["payload"]
    assert captured["payload"]["format"] == "markdown"
    # Outcome marker written for the UI/operator.
    assert (tmp_path / "tfactory_handoff.json").exists()


def test_ingest_payload_carries_contract_when_trusted(tmp_path):
    # #71 Phase 3: a trusted plan (implementation_plan.json with the tfactory
    # block / contract_version) rides along on the handoff so TFactory tests the
    # DECLARED ACs instead of inferring.
    _spec(tmp_path, True)
    (tmp_path / "implementation_plan.json").write_text(
        json.dumps(
            {
                "feature": "x",
                "contract_version": "2",
                "phases": [
                    {"name": "p", "subtasks": [{"id": "C1", "description": "d"}]}
                ],
                "tfactory": {"lanes": ["unit"], "frameworks": {"unit": "pytest"}},
            }
        )
    )
    payload = tc.build_ingest_payload(tmp_path, "001-x")
    assert "contract" in payload
    assert payload["contract"]["tfactory"]["lanes"] == ["unit"]


def test_ingest_payload_omits_contract_for_plain_plan(tmp_path):
    # AIFactory's own (create-and-run) plan has no RFC-0002 markers → no contract
    # attached → TFactory falls back to inference. Backward compatible.
    _spec(tmp_path, True)
    (tmp_path / "implementation_plan.json").write_text(
        json.dumps(
            {
                "feature": "x",
                "workflow_type": "feature",
                "phases": [
                    {"name": "p", "subtasks": [{"id": "C1", "description": "d"}]}
                ],
            }
        )
    )
    payload = tc.build_ingest_payload(tmp_path, "001-x")
    assert "contract" not in payload


def test_never_raises_on_bad_input(tmp_path, monkeypatch):
    _spec(tmp_path, True)

    async def boom(payload, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(tc, "send_handoff", boom)
    result = asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "001-x"))
    assert result["sent"] is False and result["reason"] == "error"


# ── #984: a build that wrote nothing must not be handed to verify ───────


def _repo_with_build(
    tmp: Path, spec_id: str, *, empty: bool = False, on_base: bool = False
) -> Path:
    """A project whose build worktree is a real git checkout on `dev`.

    Mirrors the live layout: spec_dir == <project>/.aifactory/specs/<spec_id>,
    build worktree == <project>/.aifactory/worktrees/tasks/<spec_id>.
    """
    project = tmp / "project"
    spec_dir = project / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "task_metadata.json").write_text(
        json.dumps({"auto_handover_tfactory": True, "base_branch": "dev"})
    )
    (spec_dir / "requirements.json").write_text(json.dumps({"title": "t"}))

    wt = project / ".aifactory" / "worktrees" / "tasks" / spec_id
    wt.mkdir(parents=True)

    def _git(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(wt), *args],  # noqa: S607
            check=True,
            capture_output=True,
        )

    _git("init", "-b", "dev", "--quiet")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (wt / "base.py").write_text("x = 1\n")
    _git("add", "-A")
    _git("commit", "-qm", "base")
    _git("update-ref", "refs/remotes/origin/dev", "dev")
    if on_base:
        # kubejob shape: the build ran inside the k8s Job, so the control-plane
        # worktree never left the base branch.
        return spec_dir
    _git("checkout", "-q", "-b", f"aifactory/{spec_id}")
    if not empty:
        (wt / "built.py").write_text("def built() -> None: ...\n")
        _git("add", "-A")
        _git("commit", "-qm", "the build")
    return spec_dir


def test_empty_build_is_not_handed_off(tmp_path, monkeypatch):
    """Zero commits vs base → refuse. Verify would otherwise test unbuilt code."""
    spec_dir = _repo_with_build(tmp_path, "005-x", empty=True)

    async def fail_send(_payload, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("handoff sent for a build that produced nothing")

    monkeypatch.setattr(tc, "send_handoff", fail_send)
    assert tc._build_commit_count(spec_dir, "005-x") == 0
    result = asyncio.run(tc.maybe_auto_handoff_tfactory(spec_dir, "005-x"))
    assert result == {"sent": False, "reason": "empty_build"}


def test_real_build_is_still_handed_off(tmp_path, monkeypatch):
    """The guard must not block a build that did write code."""
    spec_dir = _repo_with_build(tmp_path, "006-x", empty=False)
    sent = {}

    async def fake_send(payload, **_kwargs):
        sent["payload"] = payload
        return {"sent": True, "reason": None, "status": 200}

    monkeypatch.setattr(tc, "send_handoff", fake_send)
    assert tc._build_commit_count(spec_dir, "006-x") == 1
    result = asyncio.run(tc.maybe_auto_handoff_tfactory(spec_dir, "006-x"))
    assert result["sent"] is True
    assert sent["payload"]["spec_id"] == "006-x"


def test_unmeasurable_build_fails_open(tmp_path, monkeypatch):
    """No worktree (RFC-0017 packed path) → None, not 0 → handoff proceeds."""
    _spec(tmp_path, True)
    assert tc._build_commit_count(tmp_path, "007-x") is None

    async def fake_send(_payload, **_kwargs):
        return {"sent": True, "reason": None, "status": 200}

    monkeypatch.setattr(tc, "send_handoff", fake_send)
    assert (
        asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "007-x"))["sent"] is True
    )


def test_kubejob_worktree_on_base_is_unmeasurable_not_empty(tmp_path, monkeypatch):
    """The false positive that blocked a real build.

    The kubejob path builds inside the k8s Job and leaves the control-plane
    worktree on `dev`, so counting commits there returns 0 for every build.
    That must read as "cannot tell" — never as "the build wrote nothing".
    """
    spec_dir = _repo_with_build(tmp_path, "006-x", on_base=True)
    assert tc._build_commit_count(spec_dir, "006-x") is None

    async def fake_send(_payload, **_kwargs):
        return {"sent": True, "reason": None, "status": 200}

    monkeypatch.setattr(tc, "send_handoff", fake_send)
    assert (
        asyncio.run(tc.maybe_auto_handoff_tfactory(spec_dir, "006-x"))["sent"] is True
    )
