"""P2.4 — per-backend KMS round-trip tests (aws_kms / azure_kv / gcp_kms / vault_transit)."""

import os

import pytest

from tests.secrets.helpers import kms_backend_available, reimport_crypto

IN_CI = os.environ.get("CI", "").lower() == "true"

# AWS KMS test runs whenever AWS_ENDPOINT_URL is set — that signals
# LocalStack is reachable. Locally: spin LocalStack on :4566 and export
# AWS_ENDPOINT_URL=http://localhost:4566. CI's secrets-acceptance job
# sets this via the LocalStack service container.
AWS_LOCALSTACK_URL = os.environ.get("AWS_ENDPOINT_URL")


@pytest.mark.secrets
@pytest.mark.slow
@pytest.mark.skipif(
    not AWS_LOCALSTACK_URL,
    reason="AWS_ENDPOINT_URL not set; AWS KMS test requires LocalStack",
)
@pytest.mark.skipif(not kms_backend_available("aws_kms"), reason="boto3 not installed")
def test_aws_kms_roundtrip() -> None:
    """envelope-encrypt + decrypt via AWS KMS (LocalStack-backed in CI).

    Steps:
      1. Create a CMK in the LocalStack KMS endpoint (one-time per test).
      2. Re-import server.crypto with APP_KMS_BACKEND=aws_kms + AWS_KMS_KEY_ID.
      3. Wrap a fresh 32-byte data key, then unwrap.
      4. Assert plaintext round-trips and ciphertext is bigger than plaintext
         (AWS KMS' wrapped blob has metadata + auth tag — at least 64 bytes).
      5. Assert tampered ciphertext is rejected by KMS (InvalidCiphertextException).
    """
    import boto3
    from botocore.exceptions import ClientError

    # LocalStack scopes KMS keys by (account_id, region). The account id
    # is derived from the AWS_ACCESS_KEY_ID — so the fixture's client and
    # the backend's client MUST use identical credentials and region or
    # the second one won't see the key the first one created. We set the
    # env explicitly (not via setdefault) so leakage from earlier tests
    # can't shift the account id mid-test.
    os.environ["AWS_ACCESS_KEY_ID"] = "aifactory-test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "aifactory-test"
    os.environ["AWS_REGION"] = "us-east-1"

    raw_kms = boto3.client(
        "kms",
        endpoint_url=AWS_LOCALSTACK_URL,
        region_name="us-east-1",
        aws_access_key_id="aifactory-test",
        aws_secret_access_key="aifactory-test",
    )
    key_id = raw_kms.create_key(Description="aifactory-test-cmk")["KeyMetadata"]["KeyId"]

    # Sanity: same client should see the key it just created.
    listed = [k["KeyId"] for k in raw_kms.list_keys()["Keys"]]
    assert key_id in listed, f"LocalStack lost the key it just created: {listed}"

    # Now drive the backend through our factory.
    reimport_crypto({
        "APP_KMS_BACKEND": "aws_kms",
        "AWS_KMS_KEY_ID": key_id,
        "AWS_ENDPOINT_URL": AWS_LOCALSTACK_URL,
    })
    from server.crypto import get_backend  # noqa: E402

    backend = get_backend()

    # 32-byte plaintext = a typical per-org data key.
    plaintext = b"\x42" * 32
    ciphertext = backend.encrypt(plaintext)

    assert isinstance(ciphertext, bytes), "encrypt must return bytes"
    assert len(ciphertext) > len(plaintext), \
        "AWS KMS wrap should add metadata + auth tag"
    assert plaintext not in ciphertext, \
        "plaintext key bytes must not appear inside the wrapped blob"

    decrypted = backend.decrypt(ciphertext)
    assert decrypted == plaintext, "round-trip must recover the data key exactly"

    # Tamper test: flip a byte in the middle of the blob, KMS must reject.
    tampered = bytearray(ciphertext)
    tampered[len(tampered) // 2] ^= 0xFF
    with pytest.raises(ClientError) as excinfo:
        backend.decrypt(bytes(tampered))
    assert excinfo.value.response["Error"]["Code"] in {
        "InvalidCiphertextException",
        "KMSInvalidStateException",
    }, f"unexpected error code: {excinfo.value.response['Error']['Code']}"


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
