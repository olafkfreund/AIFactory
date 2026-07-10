"""Helm chart acceptance tests for the workspaces.storage toggle.

Epic #35 #40 half-B PR-2. Wires the chart-level surface for the
WorkspaceStore module from PR-1 (#173).

When ``workspaces.storage.enabled=false`` (default):
  • No WORKSPACE_S3_URI_BASE / FSSPEC_S3_* / AWS_* env on the container
  • Chart renders identically to PR-1's dev branch — no behavior change

When ``workspaces.storage.enabled=true``:
  • Operator MUST set ``uriBase`` (any fsspec URI scheme)
  • For ``s3://`` URIs, operator MUST set EITHER ``aws.useInstanceRole=true``
    (for IRSA / instance-role) OR ``aws.credentialsSecretName`` (for
    static creds in a Secret)
  • ``WORKSPACE_S3_URI_BASE`` env always injected from ``uriBase``
  • IRSA path: no AWS_* env vars (boto3 picks up the role automatically)
  • Secret path: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY via valueFrom.secretKeyRef
  • MinIO path: ``endpointUrl`` + ``addressingStyle`` add the corresponding
    FSSPEC_S3_* env vars
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
class TestWorkspaceStorageOff:
    """Default — chart byte-for-byte unchanged from PR-1 state."""

    def test_no_workspace_storage_env_vars(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = [e["name"] for e in env]
        # All four envs that the storage block injects must be absent.
        assert "WORKSPACE_S3_URI_BASE" not in names, (
            "WORKSPACE_S3_URI_BASE leaked when storage.enabled=false"
        )
        assert "AIFACTORY_S3_ENDPOINT_URL" not in names
        assert "AIFACTORY_S3_ADDRESSING_STYLE" not in names
        # AWS_* envs may exist for OTHER reasons; this test specifically
        # checks they're not sourced from the storage block. With no
        # other AWS-using features enabled in the default chart, they
        # should be absent here.
        assert "AWS_ACCESS_KEY_ID" not in names


@pytest.mark.helm
class TestWorkspaceStorageOnWithIRSA:
    """IRSA / IAM-role path — no Secret reference, app picks up creds
    via boto3's default chain (operator annotates the SA outside the
    chart). Production-recommended path."""

    def _docs(self, chart_dir, **extra) -> list[dict]:
        sets = [
            "postgres.externalSecretName=test-pg",
            "workspaces.storage.enabled=true",
            "workspaces.storage.uriBase=s3://my-bucket/workspaces",
            "workspaces.storage.aws.useInstanceRole=true",
        ]
        for k, v in extra.items():
            sets.append(f"workspaces.storage.{k}={v}")
        return _render(chart_dir, sets)

    def test_uri_base_env_injected(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        matching = [e for e in env if e["name"] == "WORKSPACE_S3_URI_BASE"]
        assert len(matching) == 1
        assert matching[0]["value"] == "s3://my-bucket/workspaces"
        # Inline value, no valueFrom (IRSA path is creds-less).
        assert "valueFrom" not in matching[0]

    def test_no_aws_creds_env_under_irsa(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """IRSA = no AWS_* envs in the pod. The Operator's SA annotation
        does the work; baking static creds in defeats the purpose."""
        dep = _find_deployment(self._docs(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert "AWS_ACCESS_KEY_ID" not in names
        assert "AWS_SECRET_ACCESS_KEY" not in names


@pytest.mark.helm
class TestWorkspaceStorageOnWithSecret:
    """Static-creds path for non-EKS / non-EC2 clusters."""

    def _docs(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "workspaces.storage.enabled=true",
                "workspaces.storage.uriBase=s3://my-bucket/workspaces",
                "workspaces.storage.aws.credentialsSecretName=aws-workspace-creds",
            ],
        )

    def test_aws_envs_from_secret_ref(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        akid = next(e for e in env if e["name"] == "AWS_ACCESS_KEY_ID")
        sak = next(e for e in env if e["name"] == "AWS_SECRET_ACCESS_KEY")
        # Both must source from the named Secret with the standard keys.
        assert akid["valueFrom"]["secretKeyRef"]["name"] == "aws-workspace-creds"
        assert akid["valueFrom"]["secretKeyRef"]["key"] == "AWS_ACCESS_KEY_ID"
        assert sak["valueFrom"]["secretKeyRef"]["name"] == "aws-workspace-creds"
        assert sak["valueFrom"]["secretKeyRef"]["key"] == "AWS_SECRET_ACCESS_KEY"


@pytest.mark.helm
class TestWorkspaceStorageMinIO:
    """MinIO / S3-compatible non-AWS — adds FSSPEC_S3_* envs."""

    def _docs(self, chart_dir, **extra) -> list[dict]:
        sets = [
            "postgres.externalSecretName=test-pg",
            "workspaces.storage.enabled=true",
            "workspaces.storage.uriBase=s3://my-minio-bucket/workspaces",
            "workspaces.storage.aws.useInstanceRole=true",
            "workspaces.storage.aws.endpointUrl=http://minio.minio:9000",
        ]
        for k, v in extra.items():
            sets.append(f"workspaces.storage.aws.{k}={v}")
        return _render(chart_dir, sets)

    def test_endpoint_url_env_injected(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        ep = next(e for e in env if e["name"] == "AIFACTORY_S3_ENDPOINT_URL")
        assert ep["value"] == "http://minio.minio:9000"

    def test_addressing_style_path_env_injected(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """MinIO usually wants path-style addressing."""
        dep = _find_deployment(self._docs(chart_dir, addressingStyle="path"))
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        style = next(e for e in env if e["name"] == "AIFACTORY_S3_ADDRESSING_STYLE")
        assert style["value"] == "path"

    def test_auto_addressing_style_not_injected(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """``auto`` is boto3's default — no need to inject the env var.
        Avoids polluting the pod env with no-op settings."""
        dep = _find_deployment(self._docs(chart_dir))  # default: auto
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert "AIFACTORY_S3_ADDRESSING_STYLE" not in names


@pytest.mark.helm
class TestWorkspaceStorageValidation:
    """Render-time validators catch the two ways an operator can
    misconfigure the storage block. Earlier than runtime failure +
    a single helm template invocation surfaces both."""

    def test_enabled_without_uri_base_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "workspaces.storage.enabled=true",
                # no uriBase
            ],
        )
        assert "workspaces.storage.enabled=true requires" in stderr
        assert "uriBase" in stderr

    def test_s3_uri_without_creds_or_irsa_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "workspaces.storage.enabled=true",
                "workspaces.storage.uriBase=s3://my-bucket/workspaces",
                # neither useInstanceRole nor credentialsSecretName
            ],
        )
        assert "uriBase=s3:// requires" in stderr
        # Names BOTH escape hatches so operator knows what to pick.
        assert "useInstanceRole" in stderr
        assert "credentialsSecretName" in stderr

    def test_non_s3_uri_does_not_require_aws_creds(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """``gs://`` or ``azure://`` URIs use their own auth chains via
        gcsfs / adlfs — the AWS-creds validator must not block them."""
        # Should NOT raise.
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "workspaces.storage.enabled=true",
                "workspaces.storage.uriBase=gs://my-gcs-bucket/workspaces",
            ],
        )
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        # uriBase env IS set; AWS_* envs are NOT.
        assert "WORKSPACE_S3_URI_BASE" in names
        assert "AWS_ACCESS_KEY_ID" not in names


@pytest.mark.helm
class TestWorkspaceStorageCoexistsWithRedis:
    """Sanity: enabling both halves of Epic #35 #40 produces a deployment
    where the env: list cleanly carries both blocks. The two blocks are
    independent template fragments but share the env: list."""

    def test_both_envs_render(self, helm_available, chart_dir) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "workspaces.storage.enabled=true",
                "workspaces.storage.uriBase=s3://my-bucket/wsp",
                "workspaces.storage.aws.useInstanceRole=true",
                "redis.enabled=true",
                "redis.url=redis://r:6379/0",
            ],
        )
        dep = _find_deployment(docs)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert "WORKSPACE_S3_URI_BASE" in names
        assert "REDIS_URL" in names
        assert "REDIS_CHANNEL" in names
