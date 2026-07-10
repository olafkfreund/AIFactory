"""Helm chart acceptance tests for the Redis pub/sub toggle.

Epic #35 #40 PR-2. The chart-level wiring for the cross-replica
event bus.

When ``redis.enabled=false`` (default):
  • No ``REDIS_URL`` or ``REDIS_CHANNEL`` env on the container
  • Chart renders identically to v1.0 — no new env, no Secret ref,
    no behavioral change

When ``redis.enabled=true``:
  • Operator MUST set EITHER ``url`` (dev/test only) OR
    ``externalSecretName`` (production)
  • ``REDIS_URL`` env injected — inline value when ``url`` set,
    Secret-ref when ``externalSecretName`` set
  • ``REDIS_CHANNEL`` env always set from ``channel`` (default
    ``aifactory:events``)
  • Helm template errors with a clear message when both sources are
    empty
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


def _render_expect_error(chart_dir, set_values: list[str] | None = None) -> str:
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


@pytest.mark.helm
class TestRedisOff:
    """Default state — no REDIS_URL wiring, byte-for-byte v1.0 chart."""

    def test_no_redis_env_vars(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = [e["name"] for e in env]
        assert "REDIS_URL" not in names, (
            "REDIS_URL leaked into the container env when redis.enabled=false"
        )
        assert "REDIS_CHANNEL" not in names


@pytest.mark.helm
class TestRedisOnWithInlineURL:
    """Toggle on + inline url — env injected from a literal value."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "redis.enabled=true",
                "redis.url=redis://test-redis:6379/0",
            ],
        )

    def test_redis_url_env_is_inline(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        matching = [e for e in env if e["name"] == "REDIS_URL"]
        assert len(matching) == 1
        assert matching[0].get("value") == "redis://test-redis:6379/0"
        # Sanity: inline URL = no valueFrom (must not silently double-source)
        assert "valueFrom" not in matching[0]

    def test_redis_channel_default(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        matching = [e for e in env if e["name"] == "REDIS_CHANNEL"]
        assert len(matching) == 1
        assert matching[0]["value"] == "aifactory:events"

    def test_redis_channel_override(self, helm_available, chart_dir) -> None:
        """Multi-AIFactory-on-one-Redis: operator overrides the channel
        per deployment to avoid cross-talk."""
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "redis.enabled=true",
                "redis.url=redis://t:6379/0",
                "redis.channel=aifactory:prod",
            ],
        )
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        ch = next(e for e in env if e["name"] == "REDIS_CHANNEL")
        assert ch["value"] == "aifactory:prod"


@pytest.mark.helm
class TestRedisOnWithExternalSecret:
    """Toggle on + externalSecretName — env injected via valueFrom."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "redis.enabled=true",
                "redis.externalSecretName=aifactory-redis",
            ],
        )

    def test_redis_url_env_from_secret_ref(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        matching = [e for e in env if e["name"] == "REDIS_URL"]
        assert len(matching) == 1
        assert "value" not in matching[0]  # not inline
        secret_ref = matching[0]["valueFrom"]["secretKeyRef"]
        assert secret_ref["name"] == "aifactory-redis"
        assert secret_ref["key"] == "REDIS_URL"


@pytest.mark.helm
class TestRedisValidation:
    """``redis.enabled=true`` MUST be accompanied by either inline URL
    or an externalSecretName. The chart's required-validator catches
    misconfiguration at ``helm template`` time, not at runtime."""

    def test_enabled_with_no_source_fails(self, helm_available, chart_dir) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "redis.enabled=true",
                # neither redis.url nor redis.externalSecretName
            ],
        )
        assert "redis.enabled=true requires" in stderr, (
            f"expected required-validator message; got stderr={stderr[:400]}"
        )
        # Sanity: the error names BOTH options so the operator knows
        # which one to set.
        assert "redis.url" in stderr
        assert "redis.externalSecretName" in stderr


@pytest.mark.helm
class TestRedisCoexistsWithRmux:
    """Sanity: turning on Redis doesn't break the rmux env block
    (they share the env: list in the container spec)."""

    def test_both_enabled_renders_both_env_blocks(
        self, helm_available, chart_dir
    ) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "redis.enabled=true",
                "redis.url=redis://r:6379/0",
                "rmux.enabled=true",
            ],
        )
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert "REDIS_URL" in names
        assert "REDIS_CHANNEL" in names
        assert "AIFACTORY_RMUX_ENABLED" in names
