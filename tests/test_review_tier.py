#!/usr/bin/env python3
"""
Tests for the review-tier classifier (Phase 4 of #397)
======================================================

Routes plans to AUTO / ASYNC / BLOCKING and sets pre_merge_gate for high-risk
paths. Pure function — no I/O.
"""

import pytest
from review_tier import (
    ASYNC,
    AUTO,
    BLOCKING,
    classify_review_tier,
)


def _plan(*subtasks: dict) -> dict:
    return {"feature": "x", "workflow_type": "feature", "phases": [{"id": "p1", "name": "P", "subtasks": list(subtasks)}]}


def _st(sid, create=None, modify=None):
    d = {"id": sid, "description": sid, "status": "pending"}
    if create:
        d["files_to_create"] = create
    if modify:
        d["files_to_modify"] = modify
    return d


class TestTiers:
    def test_trusted_plan_is_auto(self):
        a = classify_review_tier(_plan(_st("s1", ["app/x.py"])), trusted=True)
        assert a.tier == AUTO
        assert "trusted" in " ".join(a.reasons)

    def test_solo_is_auto(self):
        # 4 files (not trivial) but solo → auto.
        plan = _plan(_st("s1", ["a.py", "b.py"]), _st("s2", ["c.py", "d.py"]))
        a = classify_review_tier(plan, solo=True)
        assert a.tier == AUTO

    def test_trivial_is_auto(self):
        a = classify_review_tier(_plan(_st("s1", ["app/x.py"])))
        assert a.tier == AUTO
        assert "trivial" in " ".join(a.reasons)

    def test_standard_is_async(self):
        # Many files, not high-risk, not trivial, not trusted/solo → async.
        plan = _plan(
            _st("s1", ["app/a.py", "app/b.py"]),
            _st("s2", ["app/c.py", "app/d.py"]),
            _st("s3", ["app/e.py"]),
        )
        a = classify_review_tier(plan)
        assert a.tier == ASYNC

    def test_high_risk_is_blocking(self):
        plan = _plan(_st("s1", ["app/auth/login.py"]), _st("s2", ["app/x.py"]))
        a = classify_review_tier(plan)
        assert a.tier == BLOCKING
        assert a.pre_merge_gate is True
        assert any("auth" in f for f in a.high_risk_files)

    def test_low_confidence_is_blocking(self):
        plan = _plan(
            _st("s1", ["app/a.py", "app/b.py"]),
            _st("s2", ["app/c.py", "app/d.py"]),
            _st("s3", ["app/e.py"]),
        )
        a = classify_review_tier(plan, confidence=0.3)
        assert a.tier == BLOCKING


class TestPreMergeGate:
    def test_trusted_high_risk_auto_but_premerge_gated(self):
        # Trusted → AUTO at plan time, but high-risk still gates the merge.
        plan = _plan(_st("s1", ["migrations/001_init.py"]))
        a = classify_review_tier(plan, trusted=True)
        assert a.tier == AUTO
        assert a.pre_merge_gate is True

    def test_non_risky_has_no_premerge_gate(self):
        a = classify_review_tier(_plan(_st("s1", ["app/x.py"])))
        assert a.pre_merge_gate is False

    @pytest.mark.parametrize(
        "path",
        [
            "src/auth/session.py",
            "db/migrations/0007.sql",
            "infra/main.tf",
            "Dockerfile",
            ".github/workflows/deploy.yml",
            "billing/charge.py",
        ],
    )
    def test_high_risk_paths_detected(self, path):
        a = classify_review_tier(_plan(_st("s1", [path]), _st("s2", ["x.py"])))
        assert a.pre_merge_gate is True


def test_empty_plan_is_trivial_auto():
    a = classify_review_tier({"phases": []})
    assert a.tier == AUTO


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
