#!/usr/bin/env python3
"""
Tests for the security-reviewer pre-merge gate (Phase 5 of #397)
================================================================

Deterministic static scan over a diff's added lines + pure severity gating. The
LLM augmentation is injected and best-effort (its failure must not break the
static gate).
"""

import pytest
from agents.security_reviewer import (
    SecurityFinding,
    gate_decision,
    review_diff,
    scan_diff_static,
    severity_at_least,
)


def _diff(added_lines, file="app/x.py"):
    body = "\n".join("+" + ln for ln in added_lines)
    return f"--- a/{file}\n+++ b/{file}\n@@ -1,0 +1,{len(added_lines)} @@\n{body}\n"


class TestStaticScan:
    def test_detects_private_key(self):
        f = scan_diff_static(_diff(["-----BEGIN RSA PRIVATE KEY-----"]))
        assert any(
            x.category == "secret:private-key" and x.severity == "critical" for x in f
        )

    def test_detects_aws_key(self):
        f = scan_diff_static(_diff(["KEY = 'AKIAIOSFODNN7EXAMPLE'"]))
        assert any(x.category == "secret:aws-access-key" for x in f)

    def test_detects_hardcoded_password(self):
        f = scan_diff_static(_diff(["password = 'hunter2secret'"]))
        assert any(x.category == "secret:hardcoded" and x.severity == "high" for x in f)

    def test_detects_eval_and_shell_true(self):
        f = scan_diff_static(
            _diff(["eval(user_input)", "subprocess.run(cmd, shell=True)"])
        )
        cats = {x.category for x in f}
        assert "injection:eval" in cats
        assert "injection:shell-true" in cats

    def test_detects_unsafe_yaml_and_pickle(self):
        f = scan_diff_static(_diff(["data = yaml.load(s)", "obj = pickle.loads(b)"]))
        cats = {x.category for x in f}
        assert "deserialize:yaml-unsafe" in cats
        assert "deserialize:pickle" in cats

    def test_safe_yaml_not_flagged(self):
        f = scan_diff_static(_diff(["data = yaml.load(s, Loader=yaml.SafeLoader)"]))
        assert not any(x.category == "deserialize:yaml-unsafe" for x in f)

    def test_only_added_lines_scanned(self):
        # A removed line containing eval must NOT be flagged.
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-eval(old)\n+safe = 1\n"
        assert scan_diff_static(diff) == []

    def test_clean_diff_no_findings(self):
        assert scan_diff_static(_diff(["x = 1", "return x + 2"])) == []

    def test_line_numbers_tracked(self):
        f = scan_diff_static(_diff(["a = 1", "eval(z)"]))
        evals = [x for x in f if x.category == "injection:eval"]
        assert evals and evals[0].line == 2


class TestGateDecision:
    def test_high_finding_blocks_at_default_threshold(self):
        d = gate_decision([SecurityFinding("high", "injection:eval", "eval(x)")])
        assert d.blocked is True
        assert d.blocking

    def test_medium_does_not_block_at_high_threshold(self):
        d = gate_decision(
            [SecurityFinding("medium", "tls:verify-disabled", "verify=False")]
        )
        assert d.blocked is False

    def test_threshold_configurable(self):
        finding = [SecurityFinding("medium", "tls:verify-disabled", "verify=False")]
        assert gate_decision(finding, threshold="medium").blocked is True
        assert gate_decision(finding, threshold="high").blocked is False

    def test_no_findings_allows(self):
        d = gate_decision([])
        assert d.blocked is False

    def test_severity_at_least(self):
        assert severity_at_least("critical", "high") is True
        assert severity_at_least("low", "high") is False
        assert severity_at_least("high", "high") is True


class TestReviewDiff:
    async def test_static_only(self):
        report = await review_diff(_diff(["eval(x)"]))
        assert report.decision.blocked is True
        assert len(report.findings) == 1

    async def test_llm_augmentation_merged(self):
        async def llm(diff):
            return [
                SecurityFinding(
                    "critical", "authz:missing-check", "no owner check", source="llm"
                )
            ]

        report = await review_diff(_diff(["x = 1"]), llm_scan=llm)
        assert any(f.source == "llm" for f in report.findings)
        assert report.decision.blocked is True

    async def test_llm_failure_degrades_to_static(self):
        async def boom(diff):
            raise RuntimeError("llm down")

        # Static finds the eval; LLM crash must not break the gate.
        report = await review_diff(_diff(["eval(x)"]), llm_scan=boom)
        assert report.decision.blocked is True
        assert all(f.source == "static" for f in report.findings)

    async def test_clean_diff_allows(self):
        report = await review_diff(_diff(["return 42"]))
        assert report.decision.blocked is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
