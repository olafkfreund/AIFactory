"""Tests for the typed handback triage contract + assertion-pinning guard (#467).

Three layers:
  - ``validate_triage_report`` — the dependency-free schema gate.
  - ``assertion_guard`` — the additive-only diff-gate on AIFactory's own tests.
  - ``apply_correction`` — the seam wiring them together: a malformed triage
    report blocks the QA Fixer; a legacy markdown-only POST still runs it.
"""

from __future__ import annotations

import json
from pathlib import Path

from qa.assertion_guard import (
    count_assertions,
    guard_assertion_manifest,
    snapshot_test_assertions,
)
from qa.correction import apply_correction, check_fix_cycle_assertions
from qa.handback_contract import CONTRACT_VERSION, validate_triage_report

FIX_MD = "# QA Fix Request\n\nLogin returns 500. Fix the handler."


# ── contract validation ──────────────────────────────────────────────────────


def test_valid_triage_with_failing_tests():
    v = validate_triage_report({
        "source": "triage",
        "failing_tests": [
            {"test_id": "t1", "reason": "got 500 want 200", "file": "api.py"},
        ],
        "manifest_hash": "abc123",
        "correlation_key": "412",
    })
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
    v = validate_triage_report({
        "source": "triage",
        "failing_tests": [{"test_id": "", "reason": "r"}, {"file": "x"}],
    })
    assert v.ok is False
    assert any("test_id" in e for e in v.errors)
    assert any("reason" in e for e in v.errors)


def test_reject_nothing_to_act_on():
    v = validate_triage_report({"source": "triage", "failing_tests": []})
    assert v.ok is False
    assert any("nothing to act on" in e for e in v.errors)


def test_manifest_hash_must_be_string():
    v = validate_triage_report({
        "source": "triage",
        "failing_tests": [{"test_id": "t", "reason": "r"}],
        "manifest_hash": 12345,
    })
    assert v.ok is False


# ── assertion guard ──────────────────────────────────────────────────────────


def test_count_assertions_across_styles():
    py = "def test_x():\n    assert a == 1\n    self.assertEqual(a, b)\n    assert (c)\n"
    # bare `assert a == 1`, self.assertEqual(...), assert (c) → 3
    assert count_assertions(py) == 3
    ts = "it('x', () => { expect(a).toBe(1); assert(b); assert.equal(c, d); })"
    assert count_assertions(ts) == 3  # expect(, assert(, assert.equal( — no double count


def test_snapshot_counts_test_files(tmp_path: Path):
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n    assert 2\n")
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
        tmp_path, FIX_MD, confirm=True,
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
        spec, FIX_MD, confirm=True,
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
        tmp_path, FIX_MD, confirm=True, fixer_fn=await _fake_fixer_factory(calls),
    )
    assert res["success"] is True and res["started"] is True
    assert calls == [tmp_path]
    assert (tmp_path / "QA_FIX_REQUEST.md").read_text() == FIX_MD


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
        spec, source="triage", correlation_key="412", manifest_hash="h1",
        failing_test_count=1, triage_present=True,
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
        spec, source="triage", correlation_key="412", manifest_hash="h1",
        failing_test_count=1, triage_present=True,
    )
    test_file.write_text("def test_login():\n    assert a\n    assert b  # added\n")
    assert check_fix_cycle_assertions(spec)["ok"] is True
