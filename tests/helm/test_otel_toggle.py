"""Helm chart acceptance tests for the OpenTelemetry tracing toggle.

Epic #35 #42 PR-2. Mirrors the shape of test_redis_toggle.py +
test_workspace_storage_toggle.py.

When ``otel.enabled=false`` (default):
  • No OTEL_* env vars on the container
  • Chart renders identically to pre-#42 — no behavioural change

When ``otel.enabled=true``:
  • Operator MUST set ``otel.endpoint``
  • OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL,
    OTEL_SERVICE_NAME, OTEL_TRACES_SAMPLER, OTEL_TRACES_SAMPLER_ARG
    are all injected with the values from values.yaml
  • OTEL_EXPORTER_OTLP_HEADERS Secret-ref is added only when
    ``headersSecretName`` is set
  • Setting ``headersSecretName`` without ``enabled`` fails at helm
    template time (operator-misconfiguration trap)
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
    chart_dir,
    set_values: list[str] | None = None,
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


@pytest.mark.helm
class TestOtelOff:
    """Default — no OTel envs on the container, chart byte-for-byte
    same as pre-#42 deployments."""

    def test_no_otel_env_vars(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = [e["name"] for e in env]
        for n in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            "OTEL_SERVICE_NAME",
            "OTEL_TRACES_SAMPLER",
            "OTEL_TRACES_SAMPLER_ARG",
            "OTEL_EXPORTER_OTLP_HEADERS",
        ):
            assert n not in names, (
                f"{n} leaked into container env when otel.enabled=false"
            )


@pytest.mark.helm
class TestOtelOnMinimal:
    """Toggle on + endpoint — the five always-present env vars render."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "otel.enabled=true",
                "otel.endpoint=http://tempo.observability.svc:4317",
            ],
        )

    def test_endpoint_env_set(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        e = next(e for e in env if e["name"] == "OTEL_EXPORTER_OTLP_ENDPOINT")
        assert e["value"] == "http://tempo.observability.svc:4317"
        assert "valueFrom" not in e

    def test_protocol_default_grpc(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        e = next(e for e in env if e["name"] == "OTEL_EXPORTER_OTLP_PROTOCOL")
        assert e["value"] == "grpc"

    def test_service_name_default(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        e = next(e for e in env if e["name"] == "OTEL_SERVICE_NAME")
        assert e["value"] == "aifactory-web"

    def test_sampler_pair(self, helm_available, chart_dir) -> None:
        """ParentBased(TraceIdRatioBased(ratio)) is the SDK-spec pair.
        Default samplingRatio=1.0 keeps everything."""
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        sampler = next(e for e in env if e["name"] == "OTEL_TRACES_SAMPLER")
        arg = next(e for e in env if e["name"] == "OTEL_TRACES_SAMPLER_ARG")
        assert sampler["value"] == "parentbased_traceidratio"
        # `| quote` on 1.0 renders as "1" (yaml number serialization)
        # — either form is valid per the OTel SDK parser.
        assert arg["value"] in ("1", "1.0")

    def test_no_headers_secret_ref_when_unset(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in names


@pytest.mark.helm
class TestOtelOnWithHeadersSecret:
    """Headers Secret for vendor-auth (Honeycomb / Datadog / NewRelic)."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "otel.enabled=true",
                "otel.endpoint=https://api.honeycomb.io:443",
                "otel.protocol=http/protobuf",
                "otel.headersSecretName=honeycomb-headers",
            ],
        )

    def test_headers_env_from_secret_ref(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        h = next(e for e in env if e["name"] == "OTEL_EXPORTER_OTLP_HEADERS")
        assert "value" not in h  # not inline
        ref = h["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "honeycomb-headers"
        assert ref["key"] == "OTEL_EXPORTER_OTLP_HEADERS"

    def test_http_protobuf_protocol_passes_through(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        e = next(e for e in env if e["name"] == "OTEL_EXPORTER_OTLP_PROTOCOL")
        assert e["value"] == "http/protobuf"


@pytest.mark.helm
class TestOtelSamplingRatioZero:
    """Edge case: samplingRatio=0.0 is accepted (drops all spans at
    export). Useful for the no-export acceptance test and for keeping
    OTel wired in dev without paying export cost."""

    def test_zero_sampling_renders(self, helm_available, chart_dir) -> None:
        # `--set-json` is required because `--set foo=0.0` always
        # produces a string and the schema demands a number.
        cmd = [
            "helm",
            "template",
            "test-release",
            str(chart_dir),
            "--set",
            "postgres.externalSecretName=test-pg",
            "--set",
            "otel.enabled=true",
            "--set",
            "otel.endpoint=http://tempo:4317",
            "--set-json",
            "otel.samplingRatio=0.0",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        arg = next(e for e in env if e["name"] == "OTEL_TRACES_SAMPLER_ARG")
        # Both "0" (int-style) and "0.0" are accepted by the OTel SDK.
        assert arg["value"] in ("0", "0.0")


@pytest.mark.helm
class TestOtelValidation:
    """Required-validator + headers-without-enabled trap."""

    def test_enabled_without_endpoint_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "otel.enabled=true",
                # endpoint missing
            ],
        )
        assert "otel.enabled=true requires otel.endpoint" in stderr, (
            f"expected required-validator message; got stderr={stderr[:400]}"
        )

    def test_headers_secret_without_enabled_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """Operator typo trap: setting headersSecretName without
        enabling OTel is almost certainly a misconfiguration — the
        Secret would never be consumed. Fail loud at template time."""
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "otel.headersSecretName=tempo-headers",
                # otel.enabled left as default false
            ],
        )
        assert "otel.headersSecretName is set but otel.enabled is false" in stderr, (
            f"expected headers-without-enabled trap; got stderr={stderr[:400]}"
        )


@pytest.mark.helm
class TestOtelCoexistsWithRedisAndRmux:
    """Sanity: enabling OTel alongside the other v1.1 toggles
    (Redis + rmux + workspaces) doesn't break their env wiring."""

    def test_all_envs_render(self, helm_available, chart_dir) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "otel.enabled=true",
                "otel.endpoint=http://tempo:4317",
                "redis.enabled=true",
                "redis.url=redis://r:6379/0",
                "rmux.enabled=true",
            ],
        )
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        for n in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_TRACES_SAMPLER",
            "REDIS_URL",
            "REDIS_CHANNEL",
            "AIFACTORY_RMUX_ENABLED",
        ):
            assert n in names, f"{n} missing when toggles coexist"
