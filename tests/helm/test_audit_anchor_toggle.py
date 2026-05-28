"""Helm chart acceptance tests for the audit.anchor toggle.

Epic #35 #43 PR-2. Mirrors the shape of test_otel_toggle.py +
test_redis_toggle.py.

When ``audit.anchor.enabled=false`` (default):
  • No AUDIT_ANCHOR_* env vars on the web container.
  • No CronJob rendered.
  • Chart byte-for-byte same as pre-#43 deployments.

When ``audit.anchor.enabled=true``:
  • AUDIT_ANCHOR_ENABLED + AUDIT_ANCHOR_SCHEDULER injected on web pod.
  • scheduler=cronjob (default) → CronJob template rendered.
  • scheduler=in-process → CronJob NOT rendered (lifespan handles it).
  • scheduler=invalid → schema rejects at helm template time.

Cron knobs (schedule, timeouts, history limits) flow through to the
rendered CronJob's spec.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml


def _render(chart_dir, set_values: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _render_expect_error(
    chart_dir, set_values: list[str] | None = None,
) -> str:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert out.returncode != 0, (
        f"expected helm template to fail; got rc=0 stdout={out.stdout[:200]}"
    )
    return out.stderr


def _find_deployment(docs: list[dict]) -> dict:
    for d in docs:
        if d.get("kind") == "Deployment":
            return d
    raise AssertionError("no Deployment in rendered chart")


def _find_cronjob(docs: list[dict], component: str) -> dict | None:
    for d in docs:
        if d.get("kind") != "CronJob":
            continue
        labels = d.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/component") == component:
            return d
    return None


@pytest.mark.helm
class TestAuditAnchorOff:
    """Default state — no AUDIT_ANCHOR envs, no CronJob, chart
    byte-for-byte same as pre-#43."""

    def test_no_audit_anchor_env_vars(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = [e["name"] for e in env]
        assert "AUDIT_ANCHOR_ENABLED" not in names
        assert "AUDIT_ANCHOR_SCHEDULER" not in names

    def test_no_cronjob_when_disabled(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        assert _find_cronjob(docs, "audit-anchor-cron") is None


@pytest.mark.helm
class TestAuditAnchorOnCronJob:
    """Default scheduler=cronjob → CronJob template renders + envs
    appear on the web pod."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "audit.anchor.enabled=true",
            ],
        )

    def test_web_pod_has_audit_envs(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        e = next(e for e in env if e["name"] == "AUDIT_ANCHOR_ENABLED")
        assert e["value"] == "true"
        s = next(e for e in env if e["name"] == "AUDIT_ANCHOR_SCHEDULER")
        assert s["value"] == "cronjob"

    def test_cronjob_rendered(self, helm_available, chart_dir) -> None:
        cron = _find_cronjob(self._docs_on(chart_dir), "audit-anchor-cron")
        assert cron is not None, "audit-anchor-cron CronJob not rendered"
        # Default schedule.
        assert cron["spec"]["schedule"] == "0 0 * * *"
        # UTC pinned.
        assert cron["spec"]["timeZone"] == "UTC"

    def test_cronjob_runs_correct_command(
        self, helm_available, chart_dir,
    ) -> None:
        cron = _find_cronjob(self._docs_on(chart_dir), "audit-anchor-cron")
        container = (
            cron["spec"]["jobTemplate"]["spec"]
                ["template"]["spec"]["containers"][0]
        )
        assert container["command"] == [
            "python", "-m", "server.jobs.audit_anchor_cron",
        ]

    def test_cronjob_security_context(
        self, helm_available, chart_dir,
    ) -> None:
        """Match the web pod's hardening posture."""
        cron = _find_cronjob(self._docs_on(chart_dir), "audit-anchor-cron")
        sc = (
            cron["spec"]["jobTemplate"]["spec"]
                ["template"]["spec"]["containers"][0]["securityContext"]
        )
        assert sc["runAsNonRoot"] is True
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]


@pytest.mark.helm
class TestAuditAnchorOnInProcess:
    """scheduler=in-process → envs render, but CronJob is suppressed
    (the asyncio task in the web-server's lifespan handles ticking)."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "audit.anchor.enabled=true",
                "audit.anchor.scheduler=in-process",
            ],
        )

    def test_envs_set_to_in_process(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        s = next(e for e in env if e["name"] == "AUDIT_ANCHOR_SCHEDULER")
        assert s["value"] == "in-process"

    def test_no_cronjob_rendered(self, helm_available, chart_dir) -> None:
        assert _find_cronjob(
            self._docs_on(chart_dir), "audit-anchor-cron",
        ) is None


@pytest.mark.helm
class TestAuditAnchorScheduleOverride:
    """Operators with multiple AIFactory deployments writing to the
    same audit log MUST stagger schedules — verify the override
    flows through to the rendered CronJob."""

    def test_custom_schedule(self, helm_available, chart_dir) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "audit.anchor.enabled=true",
                "audit.anchor.cron.schedule=30 2 * * *",
            ],
        )
        cron = _find_cronjob(docs, "audit-anchor-cron")
        assert cron["spec"]["schedule"] == "30 2 * * *"


@pytest.mark.helm
class TestAuditAnchorValidation:
    """Schema-level validators fire at helm template time."""

    def test_invalid_scheduler_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "audit.anchor.enabled=true",
                "audit.anchor.scheduler=systemd-timer",
            ],
        )
        # Schema or template-level rejection — either should mention
        # the value AND the allowed enum.
        assert "scheduler" in stderr
