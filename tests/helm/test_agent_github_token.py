"""``agentGithubToken`` chart values wire the narrow agent token into the Deployment (#1973)."""

from __future__ import annotations

import subprocess

import pytest
import yaml

_PG = "postgres.externalSecretName=test-pg"
_VAR = "AIFACTORY_AGENT_GITHUB_TOKEN"
_SET = ["agentGithubToken.secretName=agent-gh", "agentGithubToken.secretKey=pat"]
_APP = [
    "githubApp.enabled=true",
    "githubApp.appId=123",
    "githubApp.installationId=456",
    "githubApp.secretName=gh-app",
]


def _render(chart_dir, set_values: list[str]) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _env(docs: list[dict]) -> list[dict]:
    for d in docs:
        if d.get("kind") == "Deployment":
            return d["spec"]["template"]["spec"]["containers"][0].get("env", [])
    raise AssertionError("no Deployment rendered")


def _entry(docs: list[dict]) -> list[dict]:
    return [e for e in _env(docs) if e["name"] == _VAR]


@pytest.mark.helm
def test_absent_by_default(helm_available, chart_dir) -> None:
    assert _entry(_render(chart_dir, [_PG])) == []


@pytest.mark.helm
@pytest.mark.parametrize(
    "extra",
    [[], _APP, ["mcpCredentials.enabled=false"]],
    ids=["plain", "github-app", "mcp-off"],
)
def test_secret_ref_when_set(helm_available, chart_dir, extra: list[str]) -> None:
    entry = _entry(_render(chart_dir, [_PG, *_SET, *extra]))
    assert len(entry) == 1
    assert "value" not in entry[0]
    assert entry[0]["valueFrom"]["secretKeyRef"] == {"name": "agent-gh", "key": "pat"}
