# ruff: noqa: S603, S607
"""``githubApp`` chart values wire the App credentials into the Deployment (#1671)."""

from __future__ import annotations

import subprocess

import pytest
import yaml

_PG = "postgres.externalSecretName=test-pg"
_ON = [
    "githubApp.enabled=true",
    "githubApp.appId=123",
    "githubApp.installationId=456",
    "githubApp.secretName=gh-app",
    "githubApp.privateKeyKey=pem",
]
_PREFIX = "AIFACTORY_GITHUB_APP_"
_KEY = "AIFACTORY_GITHUB_APP_PRIVATE_KEY"


def _cmd(chart_dir, set_values: list[str] | None) -> list[str]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    return cmd


def _render(chart_dir, set_values: list[str] | None = None) -> list[dict]:
    out = subprocess.run(
        _cmd(chart_dir, set_values), capture_output=True, text=True, check=True
    )
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _raw(chart_dir, sets: list[str] | None = None) -> str:
    return subprocess.run(
        _cmd(chart_dir, sets), capture_output=True, text=True, check=True
    ).stdout


def _render_expect_error(chart_dir, set_values: list[str] | None = None) -> str:
    out = subprocess.run(
        _cmd(chart_dir, set_values), capture_output=True, text=True, check=False
    )
    assert out.returncode != 0, (
        f"expected helm template to fail; got rc=0 stdout={out.stdout[:400]}"
    )
    return out.stderr


def _env(docs: list[dict], name: str) -> dict:
    for d in docs:
        if d.get("kind") == "Deployment":
            for e in d["spec"]["template"]["spec"]["containers"][0].get("env", []):
                if e["name"] == name:
                    return e
    raise AssertionError(f"env var {name} not present")


@pytest.mark.helm
def test_default_render_has_no_github_app_env(helm_available, chart_dir) -> None:
    assert _PREFIX not in _raw(chart_dir, [_PG])


@pytest.mark.helm
def test_enabled_render_wires_three_vars(helm_available, chart_dir) -> None:
    docs = _render(chart_dir, [_PG, *_ON])
    assert _env(docs, _PREFIX + "ID")["value"] == "123"
    assert _env(docs, _PREFIX + "INSTALLATION_ID")["value"] == "456"
    key = _env(docs, _KEY)
    assert "value" not in key
    assert key["valueFrom"]["secretKeyRef"] == {"name": "gh-app", "key": "pem"}


@pytest.mark.helm
def test_private_key_only_in_the_deployment(helm_available, chart_dir) -> None:
    sets = [_PG, *_ON, "audit.anchor.enabled=true", "tenant.isolationEnabled=true"]
    docs = _render(chart_dir, sets)
    assert any(d.get("kind") == "CronJob" for d in docs)
    assert _raw(chart_dir, sets).count(_KEY) == 1
    for d in docs:
        assert (_KEY in yaml.safe_dump(d)) == (d.get("kind") == "Deployment")
        assert not (d.get("kind") == "ConfigMap" and _KEY in yaml.safe_dump(d))


@pytest.mark.helm
def test_enabled_with_mcp_github_pat_fails(helm_available, chart_dir) -> None:
    err = _render_expect_error(
        chart_dir,
        [
            _PG,
            *_ON,
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.github=true",
        ],
    )
    assert "#1671" in err


@pytest.mark.helm
def test_enabled_with_mcp_on_but_github_off_renders(helm_available, chart_dir) -> None:
    docs = _render(
        chart_dir,
        [
            _PG,
            *_ON,
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gitlab=true",
        ],
    )
    assert docs


@pytest.mark.helm
@pytest.mark.parametrize("field", ["appId", "installationId", "secretName"])
def test_enabled_requires_field(helm_available, chart_dir, field: str) -> None:
    sets = [s for s in _ON if not s.startswith(f"githubApp.{field}=")]
    assert field in _render_expect_error(chart_dir, [_PG, *sets])
