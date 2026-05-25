"""P2.4 — per-backend KMS round-trip tests (aws_kms / azure_kv / gcp_kms / vault_transit)."""

import os

import pytest

from tests.secrets.helpers import kms_backend_available

IN_CI = os.environ.get("CI", "").lower() == "true"


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="AWS KMS requires LocalStack; CI-only")
@pytest.mark.skipif(not kms_backend_available("aws_kms"), reason="boto3 not installed")
@pytest.mark.skip(reason="P2.4 implementation pending: AWS KMS backend")
def test_aws_kms_roundtrip() -> None:
    """envelope-encrypt + decrypt via AWS KMS (LocalStack-backed in CI)."""
    pytest.fail("P2.4 not landed")


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="Azure Key Vault requires Azurite KV emulator; CI-only")
@pytest.mark.skipif(not kms_backend_available("azure_kv"), reason="azure-keyvault-keys not installed")
@pytest.mark.skip(reason="P2.4 implementation pending: Azure Key Vault backend")
def test_azure_kv_roundtrip() -> None:
    """envelope-encrypt + decrypt via Azure Key Vault (Azurite-backed in CI)."""
    pytest.fail("P2.4 not landed")


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="GCP KMS requires emulator; CI-only")
@pytest.mark.skipif(not kms_backend_available("gcp_kms"), reason="google-cloud-kms not installed")
@pytest.mark.skip(reason="P2.4 implementation pending: GCP KMS backend")
def test_gcp_kms_roundtrip() -> None:
    """envelope-encrypt + decrypt via GCP KMS (emulator-backed in CI)."""
    pytest.fail("P2.4 not landed")


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="Vault Transit requires a Vault server; CI-only")
@pytest.mark.skipif(not kms_backend_available("vault_transit"), reason="hvac not installed")
@pytest.mark.skip(reason="P2.4 implementation pending: Vault Transit backend")
def test_vault_transit_roundtrip() -> None:
    """envelope-encrypt + decrypt via HashiCorp Vault Transit (dev-mode in CI)."""
    pytest.fail("P2.4 not landed")
