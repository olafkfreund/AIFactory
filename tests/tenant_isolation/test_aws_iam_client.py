"""Tests for the AwsIamClient wrapper (Epic #35 #36 PR-2).

Covers:
  - Policy JSON shape matches design §4 EXACTLY (including the
    ``s3:prefix`` Condition).
  - IRSA trust policy targets the specific SA via the OIDC sub.
  - ``s3_recursive_delete`` enforces §4a's prefix-shape regex.
  - ``dry_run=True`` lists + counts but never calls delete_objects.
  - ``create_tenant_role`` is idempotent on ``EntityAlreadyExists``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from server.services.aws_iam_client import (
    AwsIamClient,
    AwsIamClientError,
)

pytestmark = pytest.mark.tenant_isolation

_UUID = "11111111-2222-3333-4444-555555555555"
_BUCKET = "aifactory-prod"
_OIDC_ARN = (
    "arn:aws:iam::123456789012:oidc-provider/"
    "oidc.eks.us-east-1.amazonaws.com/id/ABCDEF"
)


# ---------------------------------------------------------------------------
# Policy JSON shape
# ---------------------------------------------------------------------------


def test_build_tenant_policy_document_matches_design():
    """Policy JSON has exactly the two statements design §4 specifies.

    Including the ``s3:prefix`` StringLike condition on ListBucket —
    without it, a tenant could enumerate every key in the bucket.
    """
    doc = AwsIamClient.build_tenant_policy_document(_BUCKET, _UUID)
    assert doc["Version"] == "2012-10-17"
    statements = doc["Statement"]
    assert len(statements) == 2

    rw = next(s for s in statements if s["Sid"] == "RWWorkspace")
    assert rw["Effect"] == "Allow"
    assert set(rw["Action"]) == {
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
    }
    assert rw["Resource"] == f"arn:aws:s3:::{_BUCKET}/orgs/{_UUID}/*"

    list_ = next(s for s in statements if s["Sid"] == "ListBucketScoped")
    assert list_["Action"] == "s3:ListBucket"
    assert list_["Resource"] == f"arn:aws:s3:::{_BUCKET}"
    assert list_["Condition"]["StringLike"]["s3:prefix"] == (
        f"orgs/{_UUID}/*"
    )


def test_build_irsa_trust_policy_targets_specific_sa():
    """Trust policy's sub condition pins to the exact namespace+SA."""
    trust = AwsIamClient.build_irsa_trust_policy(
        _OIDC_ARN, "aifactory-tenant-acme",
        "aifactory-tenant-acme-agent",
    )
    stmt = trust["Statement"][0]
    assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert stmt["Principal"]["Federated"] == _OIDC_ARN
    oidc_host = "oidc.eks.us-east-1.amazonaws.com/id/ABCDEF"
    cond = stmt["Condition"]["StringEquals"]
    assert cond[f"{oidc_host}:aud"] == "sts.amazonaws.com"
    assert cond[f"{oidc_host}:sub"] == (
        "system:serviceaccount:aifactory-tenant-acme:"
        "aifactory-tenant-acme-agent"
    )


# ---------------------------------------------------------------------------
# create_tenant_role
# ---------------------------------------------------------------------------


def _mock_iam_client(*, already_exists: bool = False):
    """Build a boto3-shaped mock with the exceptions namespace."""
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException", (Exception,), {},
    )
    iam.exceptions.NoSuchEntityException = type(
        "NoSuchEntityException", (Exception,), {},
    )
    if already_exists:
        iam.create_role.side_effect = iam.exceptions.EntityAlreadyExistsException()
        iam.get_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::123:role/aifactory-tenant-{_UUID}"},
        }
    else:
        iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::123:role/aifactory-tenant-{_UUID}"},
        }
    return iam


@pytest.mark.asyncio
async def test_create_tenant_role_happy_path():
    c = AwsIamClient()
    c._iam = _mock_iam_client()

    arn = await c.create_tenant_role(
        org_uuid=_UUID, bucket=_BUCKET,
        oidc_provider_arn=_OIDC_ARN,
        sa_namespace="ns", sa_name="sa",
    )
    assert arn == f"arn:aws:iam::123:role/aifactory-tenant-{_UUID}"
    c._iam.create_role.assert_called_once()
    c._iam.put_role_policy.assert_called_once()

    # Verify the put_role_policy uses the design-§4 doc.
    kwargs = c._iam.put_role_policy.call_args.kwargs
    assert kwargs["RoleName"] == f"aifactory-tenant-{_UUID}"
    doc = json.loads(kwargs["PolicyDocument"])
    assert doc["Statement"][0]["Resource"] == (
        f"arn:aws:s3:::{_BUCKET}/orgs/{_UUID}/*"
    )


@pytest.mark.asyncio
async def test_create_tenant_role_idempotent_on_already_exists():
    """When the role exists, the trust policy is refreshed + the
    existing ARN is returned without raising."""
    c = AwsIamClient()
    c._iam = _mock_iam_client(already_exists=True)

    arn = await c.create_tenant_role(
        org_uuid=_UUID, bucket=_BUCKET,
        oidc_provider_arn=_OIDC_ARN,
        sa_namespace="ns", sa_name="sa",
    )
    assert arn.endswith(_UUID)
    # The fallback path updates the trust policy.
    c._iam.update_assume_role_policy.assert_called_once()


@pytest.mark.asyncio
async def test_create_tenant_role_other_error_wraps():
    c = AwsIamClient()
    c._iam = _mock_iam_client()
    c._iam.create_role.side_effect = RuntimeError("boom")

    with pytest.raises(AwsIamClientError):
        await c.create_tenant_role(
            org_uuid=_UUID, bucket=_BUCKET,
            oidc_provider_arn=_OIDC_ARN,
            sa_namespace="ns", sa_name="sa",
        )


# ---------------------------------------------------------------------------
# delete_tenant_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_tenant_role_idempotent_on_missing():
    c = AwsIamClient()
    c._iam = _mock_iam_client()
    c._iam.delete_role_policy.side_effect = (
        c._iam.exceptions.NoSuchEntityException()
    )
    c._iam.delete_role.side_effect = (
        c._iam.exceptions.NoSuchEntityException()
    )
    # No raise.
    await c.delete_tenant_role(_UUID)


# ---------------------------------------------------------------------------
# s3_recursive_delete — §4a safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_prefix", [
    "",
    "/",
    "orgs/",
    "orgs/12345/",                                    # not a UUID
    "orgs/11111111-2222-3333-4444-555555555555",      # missing trailing slash
    "../orgs/11111111-2222-3333-4444-555555555555/",  # path-traversal-ish
    "tenants/11111111-2222-3333-4444-555555555555/",  # wrong root segment
])
async def test_s3_recursive_delete_rejects_bad_prefix(bad_prefix):
    """The §4a regex must reject any prefix that isn't UUID-shaped."""
    c = AwsIamClient()
    c._s3 = MagicMock()
    with pytest.raises(ValueError):
        await c.s3_recursive_delete(
            bucket=_BUCKET, prefix=bad_prefix, dry_run=False,
        )
    # Critical: the API client must NEVER be touched on a bad prefix.
    c._s3.get_paginator.assert_not_called()


@pytest.mark.asyncio
async def test_s3_recursive_delete_accepts_good_prefix():
    """A UUID-shaped prefix gets through."""
    c = AwsIamClient()
    s3 = MagicMock()
    pages = MagicMock()
    pages.paginate.return_value = [
        {"Contents": [{"Key": f"orgs/{_UUID}/a.txt"}]},
        {"Contents": [{"Key": f"orgs/{_UUID}/b.txt"}]},
    ]
    s3.get_paginator.return_value = pages
    c._s3 = s3

    count = await c.s3_recursive_delete(
        bucket=_BUCKET, prefix=f"orgs/{_UUID}/", dry_run=False,
    )
    assert count == 2
    s3.delete_objects.assert_called()


@pytest.mark.asyncio
async def test_s3_recursive_delete_dry_run_does_not_delete():
    """dry_run=True lists + counts, never calls delete_objects."""
    c = AwsIamClient()
    s3 = MagicMock()
    pages = MagicMock()
    pages.paginate.return_value = [
        {"Contents": [{"Key": f"orgs/{_UUID}/x"}, {"Key": f"orgs/{_UUID}/y"}]},
    ]
    s3.get_paginator.return_value = pages
    c._s3 = s3

    count = await c.s3_recursive_delete(
        bucket=_BUCKET, prefix=f"orgs/{_UUID}/", dry_run=True,
    )
    assert count == 2
    s3.delete_objects.assert_not_called()


@pytest.mark.asyncio
async def test_s3_recursive_delete_empty_prefix_is_zero():
    """Empty prefix (no Contents) returns 0 + doesn't crash."""
    c = AwsIamClient()
    s3 = MagicMock()
    pages = MagicMock()
    pages.paginate.return_value = [{"Contents": []}]
    s3.get_paginator.return_value = pages
    c._s3 = s3

    count = await c.s3_recursive_delete(
        bucket=_BUCKET, prefix=f"orgs/{_UUID}/", dry_run=False,
    )
    assert count == 0
    s3.delete_objects.assert_not_called()
