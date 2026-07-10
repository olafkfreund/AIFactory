"""AWS IAM + S3 wrapper for tenant reconciler (Epic #35 #36 PR-2).

What this ships
---------------

Per-tenant IAM role creation with the hard-coded-prefix policy from
design §4. We deliberately picked one-role-per-tenant over the older
``${aws:PrincipalTag/...}`` approach because tag interpolation needs
a mutating webhook + reliable session tags at AssumeRole time —
which the EKS IRSA path doesn't natively provide.

Operational trade-off: N orgs = N roles, AWS soft-limit 5,000 roles
per account. >500 orgs → request the limit increase or migrate to
the session-tag approach (future work).

Safety: §4a's prefix-shape assertion
-----------------------------------

``s3_recursive_delete`` REFUSES to delete any prefix that doesn't
match ``^orgs/[0-9a-f-]{36}/$``. The reconciler's tear-down stage-2
also runs a 24-hour dry-run pass before the actual delete (driven
by the daily cron job in PR-3).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# UUID-shaped prefix the §4a safety check accepts. The trailing slash
# is mandatory so we can never accidentally match ``orgs/<uuid>-extra``.
_TENANT_PREFIX_RE = re.compile(r"^orgs/[0-9a-f-]{36}/$")


class AwsIamClientError(RuntimeError):
    """Raised when an IAM/S3 call fails for non-idempotent reasons."""


class AwsIamClient:
    """boto3-backed wrapper.

    boto3 itself is synchronous; we wrap each call in
    ``asyncio.to_thread`` so the reconciler's loop stays non-blocking.
    The reconciler is the only caller and runs at low frequency
    (5-min sweep + on-org-create) so thread-pool exhaustion is not a
    concern.
    """

    def __init__(self) -> None:
        self._iam = None  # type: ignore[assignment]
        self._s3 = None  # type: ignore[assignment]

    # -- Lazy client init ----------------------------------------------

    def _iam_client(self) -> Any:
        if self._iam is None:
            import boto3

            self._iam = boto3.client("iam")
        return self._iam

    def _s3_client(self) -> Any:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    # -- Policy + trust-policy builders --------------------------------

    @staticmethod
    def build_tenant_policy_document(
        bucket: str,
        org_uuid: str,
    ) -> dict[str, Any]:
        """Return the IAM policy JSON for one tenant (design §4).

        Hard-coded ``Resource`` ARN + ``s3:prefix`` condition so the
        IAM evaluation engine — not the application — enforces the
        per-tenant isolation. Cross-tenant access fails with
        ``AccessDenied`` at the AWS API level.
        """
        prefix = f"orgs/{org_uuid}/"
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RWWorkspace",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                    ],
                    "Resource": f"arn:aws:s3:::{bucket}/{prefix}*",
                },
                {
                    "Sid": "ListBucketScoped",
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": f"arn:aws:s3:::{bucket}",
                    "Condition": {
                        "StringLike": {"s3:prefix": f"{prefix}*"},
                    },
                },
            ],
        }

    @staticmethod
    def build_irsa_trust_policy(
        oidc_provider_arn: str,
        sa_namespace: str,
        sa_name: str,
    ) -> dict[str, Any]:
        """Return the IRSA OIDC trust policy for one SA.

        ``oidc_provider_arn`` is the EKS cluster's OIDC provider; the
        ``sub`` condition is what restricts the role to a specific
        ServiceAccount in a specific namespace.
        """
        oidc_host = oidc_provider_arn.split("oidc-provider/", 1)[-1]
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Federated": oidc_provider_arn},
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                            f"{oidc_host}:aud": "sts.amazonaws.com",
                            f"{oidc_host}:sub": (
                                f"system:serviceaccount:{sa_namespace}:{sa_name}"
                            ),
                        },
                    },
                }
            ],
        }

    @staticmethod
    def _role_name(org_uuid: str) -> str:
        # IAM role name max 64 chars; "aifactory-tenant-" (17) + uuid
        # (36) = 53 chars. Safe.
        return f"aifactory-tenant-{org_uuid}"

    @staticmethod
    def _policy_name(org_uuid: str) -> str:
        return f"aifactory-tenant-{org_uuid}-s3"

    # -- IAM role lifecycle --------------------------------------------

    async def create_tenant_role(
        self,
        *,
        org_uuid: str,
        bucket: str,
        oidc_provider_arn: str,
        sa_namespace: str,
        sa_name: str,
    ) -> str:
        """Create the role + attach the inline S3 policy. Returns role ARN.

        Idempotent on ``EntityAlreadyExists`` — the role's policy is
        re-PUT each time so a config drift (e.g. bucket renamed) is
        self-correcting next reconcile pass.
        """
        import asyncio

        role_name = self._role_name(org_uuid)
        policy_name = self._policy_name(org_uuid)
        trust = self.build_irsa_trust_policy(
            oidc_provider_arn,
            sa_namespace,
            sa_name,
        )
        policy = self.build_tenant_policy_document(bucket, org_uuid)

        iam = self._iam_client()

        def _create_role() -> str:
            try:
                resp = iam.create_role(
                    RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(trust),
                    Description=(
                        f"AIFactory tenant {org_uuid} — per-tenant "
                        f"S3 access. Managed by tenant_reconciler."
                    ),
                )
                return resp["Role"]["Arn"]
            except iam.exceptions.EntityAlreadyExistsException:
                # Refresh trust policy in case the SA was recreated
                # (rare; reconcilable without operator intervention).
                iam.update_assume_role_policy(
                    RoleName=role_name,
                    PolicyDocument=json.dumps(trust),
                )
                return iam.get_role(RoleName=role_name)["Role"]["Arn"]

        def _put_policy() -> None:
            iam.put_role_policy(
                RoleName=role_name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy),
            )

        try:
            arn = await asyncio.to_thread(_create_role)
            await asyncio.to_thread(_put_policy)
        except Exception as exc:
            raise AwsIamClientError(
                f"create_tenant_role({org_uuid}) failed: {exc}",
            ) from exc
        return arn

    async def delete_tenant_role(self, org_uuid: str) -> None:
        """Delete the role + its inline policy. Idempotent on missing."""
        import asyncio

        role_name = self._role_name(org_uuid)
        policy_name = self._policy_name(org_uuid)
        iam = self._iam_client()

        def _delete() -> None:
            # Inline policy must be deleted before the role.
            try:
                iam.delete_role_policy(
                    RoleName=role_name,
                    PolicyName=policy_name,
                )
            except iam.exceptions.NoSuchEntityException:
                pass
            try:
                iam.delete_role(RoleName=role_name)
            except iam.exceptions.NoSuchEntityException:
                pass

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            raise AwsIamClientError(
                f"delete_tenant_role({org_uuid}) failed: {exc}",
            ) from exc

    # -- S3 recursive delete (with §4a safety) -------------------------

    async def s3_recursive_delete(
        self,
        bucket: str,
        prefix: str,
        dry_run: bool,
    ) -> int:
        """Delete every object under ``s3://bucket/prefix``.

        §4a safety: prefix MUST match ``^orgs/[0-9a-f-]{36}/$`` or
        we raise ``ValueError``. Bad prefixes cannot reach the AWS
        API. ``dry_run=True`` lists + counts only — the 24-hour dry-
        run pass before the real delete is the operator's chance to
        intervene.

        Returns the number of objects (deleted, or that WOULD be
        deleted in dry-run mode).
        """
        import asyncio

        # WHY: even an operator-fat-fingered call with prefix="" or
        # prefix="orgs/" would wipe other tenants. The regex is the
        # last line of defence — never relax it without revisiting
        # the tear-down threat model.
        if not _TENANT_PREFIX_RE.match(prefix):
            raise ValueError(
                f"refusing recursive delete: prefix={prefix!r} does not "
                f"match {_TENANT_PREFIX_RE.pattern!r}",
            )

        s3 = self._s3_client()

        def _list_and_delete() -> int:
            count = 0
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", []) or []]
                if not keys:
                    continue
                count += len(keys)
                if dry_run:
                    logger.info(
                        "dry-run s3 delete: bucket=%s prefix=%s would "
                        "delete %d keys (first: %r)",
                        bucket,
                        prefix,
                        len(keys),
                        keys[0]["Key"],
                    )
                    continue
                s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": keys, "Quiet": True},
                )
            return count

        try:
            return await asyncio.to_thread(_list_and_delete)
        except Exception as exc:
            raise AwsIamClientError(
                f"s3_recursive_delete({bucket}/{prefix}) failed: {exc}",
            ) from exc
