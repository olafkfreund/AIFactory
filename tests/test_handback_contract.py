"""Tests for the typed handback triage contract + assertion-pinning guard (#467).

Three layers:
  - ``validate_triage_report`` — the dependency-free schema gate.
  - ``assertion_guard`` — the additive-only diff-gate on AIFactory's own tests.
  - ``apply_correction`` — the seam wiring them together: a malformed triage
    report blocks the QA Fixer; a legacy markdown-only POST still runs it.
"""

# ruff: noqa: S603, S607

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from pfactory import tfactory_client
from qa.assertion_guard import (
    count_assertions,
    guard_assertion_manifest,
    snapshot_test_assertions,
)
from qa.correction import (
    apply_correction,
    check_fix_cycle_assertions,
    publish_correction,
    snapshot_correction_workspace,
)
from qa.handback_contract import CONTRACT_VERSION, validate_triage_report

FIX_MD = "# QA Fix Request\n\nLogin returns 500. Fix the handler."


# ── contract validation ──────────────────────────────────────────────────────


def test_valid_triage_with_failing_tests():
    v = validate_triage_report(
        {
            "source": "triage",
            "failing_tests": [
                {"test_id": "t1", "reason": "got 500 want 200", "file": "api.py"},
            ],
            "manifest_hash": "abc123",
            "correlation_key": "412",
        }
    )
    assert v.ok is True
    assert v.errors == []
    assert v.failing_test_count == 1
    assert v.manifest_hash == "abc123"
    assert v.correlation_key == "412"
    assert v.contract_version == CONTRACT_VERSION


def test_valid_visual_inspection_without_failing_tests():
    v = validate_triage_report({"source": "visual_inspection", "has_visual_plan": True})
    assert v.ok is True


def test_reject_non_object():
    assert validate_triage_report("nope").ok is False
    assert validate_triage_report(None).ok is False


def test_reject_missing_source():
    v = validate_triage_report({"failing_tests": [{"test_id": "t", "reason": "r"}]})
    assert v.ok is False
    assert any("source" in e for e in v.errors)


def test_reject_failing_test_missing_fields():
    v = validate_triage_report(
        {
            "source": "triage",
            "failing_tests": [{"test_id": "", "reason": "r"}, {"file": "x"}],
        }
    )
    assert v.ok is False
    assert any("test_id" in e for e in v.errors)
    assert any("reason" in e for e in v.errors)


def test_reject_nothing_to_act_on():
    v = validate_triage_report({"source": "triage", "failing_tests": []})
    assert v.ok is False
    assert any("nothing to act on" in e for e in v.errors)


def test_manifest_hash_must_be_string():
    v = validate_triage_report(
        {
            "source": "triage",
            "failing_tests": [{"test_id": "t", "reason": "r"}],
            "manifest_hash": 12345,
        }
    )
    assert v.ok is False


# ── assertion guard ──────────────────────────────────────────────────────────


def test_count_assertions_across_styles():
    py = (
        "def test_x():\n    assert a == 1\n    self.assertEqual(a, b)\n    assert (c)\n"
    )
    # bare `assert a == 1`, self.assertEqual(...), assert (c) → 3
    assert count_assertions(py) == 3
    ts = "it('x', () => { expect(a).toBe(1); assert(b); assert.equal(c, d); })"
    assert (
        count_assertions(ts) == 3
    )  # expect(, assert(, assert.equal( — no double count


def test_snapshot_counts_test_files(tmp_path: Path):
    (tmp_path / "test_a.py").write_text(
        "def test_a():\n    assert 1 == 1\n    assert 2\n"
    )
    (tmp_path / "b_test.py").write_text("def test_b():\n    assert True\n")
    (tmp_path / "helpers.py").write_text("assert 9  # not a test file\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "test_skip.py").write_text("assert 1\n")
    snap = snapshot_test_assertions(tmp_path)
    assert snap == {"test_a.py": 2, "b_test.py": 1}  # prunes non-tests + node_modules


def test_guard_flags_reduced_and_removed():
    before = {"test_a.py": 3, "test_b.py": 2}
    after = {"test_a.py": 1}  # a weakened, b removed
    report = guard_assertion_manifest(before, after)
    assert report.ok is False
    kinds = {v.path: v.kind for v in report.violations}
    assert kinds == {"test_a.py": "assertions_reduced", "test_b.py": "file_removed"}


def test_guard_allows_additive_changes():
    before = {"test_a.py": 2}
    after = {"test_a.py": 5, "test_new.py": 3}  # more asserts + new file
    assert guard_assertion_manifest(before, after).ok is True


# ── apply_correction integration ─────────────────────────────────────────────


async def _fake_fixer_factory(calls):
    async def fake_fixer(spec_dir):
        calls.append(spec_dir)
        return {"status": "qa_fixing"}

    return fake_fixer


async def test_malformed_triage_blocks_the_fixer(tmp_path: Path):
    calls: list = []
    res = await apply_correction(
        tmp_path,
        FIX_MD,
        confirm=True,
        fixer_fn=await _fake_fixer_factory(calls),
        triage={"source": "triage", "failing_tests": []},  # nothing to act on
        correlation_key="412",
    )
    assert res["success"] is False
    assert res["rejected"] is True
    assert res["started"] is False
    assert res["correlation_key"] == "412"
    assert res["validation_errors"]
    assert calls == []  # fixer never ran
    assert not (tmp_path / "QA_FIX_REQUEST.md").exists()  # nothing written


async def test_valid_triage_runs_fixer_and_records_manifest(tmp_path: Path):
    # A realistic spec layout so the project-root walk is well-defined.
    spec = tmp_path / "proj" / ".aifactory" / "specs" / "001"
    spec.mkdir(parents=True)
    calls: list = []
    res = await apply_correction(
        spec,
        FIX_MD,
        confirm=True,
        fixer_fn=await _fake_fixer_factory(calls),
        triage={
            "source": "triage",
            "failing_tests": [{"test_id": "t1", "reason": "500 not 200"}],
            "manifest_hash": "deadbeef",
            "correlation_key": "412",
        },
    )
    assert res["success"] is True and res["started"] is True
    assert res["manifest_hash"] == "deadbeef"
    assert res["correlation_key"] == "412"
    assert calls == [spec]
    record = json.loads((spec / "handback_received.json").read_text())
    assert record["manifest_hash"] == "deadbeef"
    assert record["correlation_key"] == "412"
    assert record["failing_test_count"] == 1
    assert "assertion_baseline" in record  # baseline captured for the guard


async def test_legacy_markdown_only_still_runs(tmp_path: Path):
    """Non-breaking: no triage block → today's behaviour, fixer runs."""
    calls: list = []
    res = await apply_correction(
        tmp_path,
        FIX_MD,
        confirm=True,
        fixer_fn=await _fake_fixer_factory(calls),
    )
    assert res["success"] is True and res["started"] is True
    assert calls == [tmp_path]
    assert (tmp_path / "QA_FIX_REQUEST.md").read_text() == FIX_MD


@pytest.mark.asyncio
async def test_non_adopted_handback_fixer_uses_project_without_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import phase_config
    from core import client as client_module
    from core import contract_trust
    from qa import correction, fixer

    spec = tmp_path / ".aifactory/specs/001"
    spec.mkdir(parents=True)
    (spec / "handback_received.json").write_text('{"tfactory_task_id":"t1"}')
    selected = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    def create(project_dir, *_args, **_kwargs):
        selected.append(project_dir)
        return Client()

    async def run(*_args, **_kwargs):
        return "fixed", None

    async def publish(*_args):
        return True

    monkeypatch.setattr(
        contract_trust, "trusted_contract", lambda *_a: ("legacy", None, "")
    )
    monkeypatch.setattr(client_module, "create_client", create)
    monkeypatch.setattr(phase_config, "get_phase_model", lambda *_a: "claude")
    monkeypatch.setattr(phase_config, "get_phase_thinking_budget", lambda *_a: None)
    monkeypatch.setattr(phase_config, "infer_provider_from_model", lambda *_a: "claude")
    monkeypatch.setattr(fixer, "run_qa_fixer_session", run)
    monkeypatch.setattr(correction, "publish_correction", publish)
    assert await correction._run_fixer_bg(spec)
    assert selected == [tmp_path]


def test_check_fix_cycle_flags_weakened_assertions(tmp_path: Path):
    """End-to-end guard: baseline recorded, then a test loses an assertion."""
    proj = tmp_path / "proj"
    spec = proj / ".aifactory" / "specs" / "001"
    spec.mkdir(parents=True)
    test_file = proj / "test_login.py"
    test_file.write_text("def test_login():\n    assert a\n    assert b\n")

    # Record a handback baseline (snapshots the project's test assertions now).
    from qa.correction import record_handback

    record_handback(
        spec,
        source="triage",
        correlation_key="412",
        manifest_hash="h1",
        failing_test_count=1,
        triage_present=True,
    )

    # The fixer weakens the test to make it pass.
    test_file.write_text("def test_login():\n    assert a\n")  # dropped one assert

    report = check_fix_cycle_assertions(spec)
    assert report["ok"] is False
    assert report["correlation_key"] == "412"
    assert report["violations"][0]["path"] == "test_login.py"
    assert report["violations"][0]["kind"] == "assertions_reduced"


def test_check_fix_cycle_ok_when_additive(tmp_path: Path):
    proj = tmp_path / "proj"
    spec = proj / ".aifactory" / "specs" / "001"
    spec.mkdir(parents=True)
    test_file = proj / "test_login.py"
    test_file.write_text("def test_login():\n    assert a\n")

    from qa.correction import record_handback

    record_handback(
        spec,
        source="triage",
        correlation_key="412",
        manifest_hash="h1",
        failing_test_count=1,
        triage_present=True,
    )
    test_file.write_text("def test_login():\n    assert a\n    assert b  # added\n")
    assert check_fix_cycle_assertions(spec)["ok"] is True


def test_guard_checks_correction_checkout(tmp_path: Path):
    spec = tmp_path / "project/.aifactory/specs/001"
    spec.mkdir(parents=True)
    correction = tmp_path / "correction"
    tests = correction / "tests"
    tests.mkdir(parents=True)
    test_file = tests / "test_example.py"
    test_file.write_text("def test_example():\n    assert True\n")
    from qa.correction import record_handback

    record_handback(
        spec,
        source="triage",
        correlation_key="x",
        manifest_hash=None,
        failing_test_count=1,
        triage_present=True,
    )
    snapshot_correction_workspace(spec, correction)
    test_file.write_text("def test_example():\n    pass\n")
    assert check_fix_cycle_assertions(spec, correction)["ok"] is False


async def test_duplicate_handoff_does_not_rerun_fixer(tmp_path: Path):
    spec = tmp_path / "proj/.aifactory/specs/001"
    spec.mkdir(parents=True)
    calls: list[Path] = []

    async def fixer(path: Path) -> dict:
        calls.append(path)
        return {"status": "qa_fixing"}

    for _ in range(2):
        result = await apply_correction(
            spec,
            FIX_MD,
            confirm=True,
            fixer_fn=fixer,
            manifest_hash="manifest",
            tfactory_task_id="tf-1",
        )
    assert result["duplicate"] is True
    assert calls == [spec]
    receipt = json.loads((spec / "handback_received.json").read_text())
    assert receipt["tfactory_task_id"] == "tf-1"
    assert receipt["pre_fix_sha"] is None


async def test_tested_sha_reservation_dedups_concurrent_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = tmp_path / "proj/.aifactory/specs/001"
    spec.mkdir(parents=True)
    calls = 0

    async def fixer(_path: Path) -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"scheduled": True}

    results = await asyncio.gather(
        *(
            apply_correction(
                spec,
                FIX_MD,
                confirm=True,
                fixer_fn=fixer,
                tfactory_task_id="tf-1",
                tested_sha="A" * 40,
            )
            for _ in range(2)
        )
    )
    assert calls == 1
    assert sum(bool(result.get("duplicate")) for result in results) == 1
    assert (
        json.loads((spec / "handback_received.json").read_text())["pre_fix_sha"]
        == "a" * 40
    )
    (spec / "handback_pushed.json").write_text(json.dumps({"head_sha": "b" * 40}))
    delivered = []

    async def callback(_spec: Path, _id: str, *, head_sha: str) -> dict:
        delivered.append(head_sha)
        return {"sent": True}

    monkeypatch.setattr(tfactory_client, "send_fix_completed", callback)
    await apply_correction(
        spec,
        FIX_MD,
        confirm=True,
        fixer_fn=fixer,
        tfactory_task_id="tf-1",
        tested_sha="a" * 40,
    )
    assert delivered == ["b" * 40] and calls == 1
    assert not (spec / "handback_pushed.json").exists()


async def test_fix_completed_posts_configured_url_once(monkeypatch, tmp_path: Path):
    spec = tmp_path / "proj/.aifactory/specs/001"
    spec.mkdir(parents=True)
    (spec / "handback_received.json").write_text(
        json.dumps(
            {
                "tfactory_task_id": "tf-1",
                "pre_fix_sha": "a" * 40,
                "correlation_key": "2172",
            }
        )
    )
    (spec / "task_metadata.json").write_text(
        json.dumps(
            {
                "project_id": "project",
                "source_branch": "factory/2172-test",
                "pr_number": 7,
            }
        )
    )
    (spec / "context").mkdir()
    (spec / "context/task_contract.json").write_text(
        json.dumps(
            {"contract_version": "2", "provenance": {"project_id": "signed-project"}}
        )
    )
    monkeypatch.setenv("TFACTORY_BASE_URL", "https://configured.example")
    monkeypatch.setenv("TFACTORY_HANDBACK_SECRET", "test-token")
    calls: list[tuple] = []

    async def poster(url: str, payload: dict, headers: dict) -> dict:
        calls.append((url, payload, headers))
        return {"ok": True, "status": 200}

    assert (
        await tfactory_client.send_fix_completed(
            spec, "001", poster=poster, head_sha="A" * 40
        )
    )["reason"] == "unchanged"
    assert (
        await tfactory_client.send_fix_completed(
            spec, "001", poster=poster, head_sha="B" * 40
        )
    )["sent"] is True
    assert (
        await tfactory_client.send_fix_completed(
            spec, "001", poster=poster, head_sha="b" * 40
        )
    )["reason"] == "unchanged"
    assert len(calls) == 1
    assert calls[0][0] == "https://configured.example/api/handback/aifactory-complete"
    assert calls[0][1] == {
        "tfactory_task_id": "tf-1",
        "project_id": "signed-project",
        "spec_id": "001",
        "correlation_key": "2172",
        "source_branch": "factory/2172-test",
        "head_sha": "b" * 40,
        "pr_number": 7,
        "status": "complete",
    }
    assert calls[0][2]["X-TFactory-Handback-Token"] == "test-token"
    assert calls[0][2]["Idempotency-Key"]


async def test_publish_correction_pushes_task_branch(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    project = tmp_path / "project"
    remote = tmp_path / "remote.git"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "factory/2172-test", str(project)], check=True
    )
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    (project / "app.py").write_text("before\n")
    subprocess.run(["git", "-C", str(project), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", str(remote)], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "push",
            "origin",
            "HEAD:refs/heads/factory/2172-test",
        ],
        check=True,
    )
    spec = project / ".aifactory/specs/001"
    spec.mkdir(parents=True)
    (project / "app.py").write_text("after\n")
    sent: list[str] = []

    async def callback(_spec: Path, _id: str, *, head_sha: str) -> dict:
        sent.append(head_sha)
        return {"sent": True}

    monkeypatch.setattr(tfactory_client, "send_fix_completed", callback)
    assert await publish_correction(spec, project, "factory/2172-test", ["app.py"])
    remote_head = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "refs/heads/factory/2172-test"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sent == [remote_head]


async def test_failed_callback_retries_with_same_idempotency_key(
    monkeypatch, tmp_path: Path
):
    spec = tmp_path / "proj/.aifactory/specs/001"
    spec.mkdir(parents=True)
    (spec / "handback_received.json").write_text(
        json.dumps({"tfactory_task_id": "tf-1", "pre_fix_sha": "a" * 40})
    )
    (spec / "task_metadata.json").write_text(
        json.dumps({"source_branch": "factory/test"})
    )
    monkeypatch.setenv("TFACTORY_BASE_URL", "https://configured.example")
    monkeypatch.setenv("TFACTORY_HANDBACK_SECRET", "token")
    keys = []

    async def poster(_url: str, _payload: dict, headers: dict) -> dict:
        keys.append(headers["Idempotency-Key"])
        return {"ok": len(keys) == 2, "status": 200 if len(keys) == 2 else 503}

    first = await tfactory_client.send_fix_completed(
        spec, "001", poster=poster, head_sha="b" * 40
    )
    assert first["sent"] is False
    second = await tfactory_client.send_fix_completed(
        spec, "001", poster=poster, head_sha="B" * 40
    )
    assert second["sent"] is True
    assert keys[0] == keys[1]

    (spec / "handback_sent.json").write_text(
        json.dumps(
            {"sent_shas": [], "pending": ["c" * 40], "pending_at": {"c" * 40: 0}}
        )
    )
    third = await tfactory_client.send_fix_completed(
        spec, "001", poster=poster, head_sha="c" * 40
    )
    assert third["sent"] is False  # the expired reservation was retried
    assert len(keys) == 3

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_poster(_url: str, _payload: dict, _headers: dict) -> dict:
        started.set()
        await release.wait()
        return {"ok": True, "status": 200}

    first_delivery = asyncio.create_task(
        tfactory_client.send_fix_completed(
            spec, "001", poster=slow_poster, head_sha="d" * 40
        )
    )
    await started.wait()
    duplicate = await tfactory_client.send_fix_completed(
        spec, "001", poster=slow_poster, head_sha="d" * 40
    )
    assert duplicate["reason"] == "in_flight"
    release.set()
    assert (await first_delivery)["sent"] is True


async def test_publish_retries_callback_without_a_second_commit(
    monkeypatch, tmp_path: Path
):
    project = tmp_path / "project"
    remote = tmp_path / "remote.git"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "factory/test", str(project)], check=True
    )
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    (project / "app.py").write_text("before\n")
    subprocess.run(["git", "-C", str(project), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", str(remote)], check=True
    )
    spec = project / ".aifactory/specs/001"
    spec.mkdir(parents=True)
    (project / "app.py").write_text("after\n")
    sent = []

    async def callback(_spec: Path, _id: str, *, head_sha: str) -> dict:
        sent.append(head_sha)
        return {"sent": len(sent) == 2}

    monkeypatch.setattr(tfactory_client, "send_fix_completed", callback)
    assert not await publish_correction(spec, project, "factory/test", ["app.py"])
    pushed_sha = json.loads((spec / "handback_pushed.json").read_text())["head_sha"]
    assert await publish_correction(spec, project, "factory/test", ["app.py"])
    assert sent == [pushed_sha, pushed_sha]
    assert not (spec / "handback_pushed.json").exists()
