"""The app pod must always receive a KMS key (#1276).

This test exists to hold up an assumption made somewhere else. #1276 encrypts
the JSON credential stores (claude-profiles.json, api-profiles.json,
settings.json) with the repo's existing crypto.kms backend -- but
``secret_field.seal()`` deliberately DEGRADES to writing plaintext, with a
one-time warning, when no key is configured. An operator with no key must still
be able to save a profile, and is no worse off than before that change.

Which means "the credential stores are encrypted at rest" is only true while a
key actually reaches the pod. Today the chart wires ``KMS_FERNET_KEY`` from the
``aifactory-kms`` Secret by default, which is why it holds -- but a chart
default is exactly the kind of thing that gets changed in a values file two
quarters from now, and the failure mode is silent: tokens go back to plaintext
and only a log line says so. A comment cannot catch that. This can.

If you are here because this test failed, the fix is to restore the key wiring,
not to weaken the assertion. See also #1290 (only the fernet backend is wired).
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

# Every backend the chart supports, and the env var each one needs at the pod
# for crypto.kms.get_backend() to return something that can actually encrypt.
BACKEND_ENV = {
    "fernet": "KMS_FERNET_KEY",
    "aws_kms": "APP_KMS_AWS_KEY_ID",
    "vault_transit": "APP_KMS_VAULT_TRANSIT_KEY",
    "azure_kv": "APP_KMS_AZURE_KEYVAULT_URL",
    "gcp_kms": "APP_KMS_GCP_KEY_NAME",
}


def _render(chart_dir, set_values: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _app_container(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if "aifactory" in container["name"] or container["name"] == "app":
                return container
    pytest.fail("no app container found in the rendered chart")
    raise AssertionError  # unreachable, keeps the return type honest


@pytest.mark.helm
def test_default_render_gives_the_app_a_fernet_key(helm_available, chart_dir):
    """The out-of-the-box chart must hand the pod a key, not hope for one."""
    if not helm_available:
        pytest.skip("helm not installed")

    container = _app_container(_render(chart_dir))
    env = {e["name"]: e for e in container.get("env", [])}

    assert "KMS_FERNET_KEY" in env, (
        "the default render no longer gives the app a KMS key -- the JSON "
        "credential stores would silently fall back to writing plaintext. "
        "See the module docstring."
    )
    # From a Secret, never a literal: a key in values.yaml would land in
    # `helm get values` and in whatever git repo holds the release.
    assert "secretKeyRef" in env["KMS_FERNET_KEY"].get("valueFrom", {}), (
        "KMS_FERNET_KEY must come from a Secret reference, not an inline value"
    )
    assert "value" not in env["KMS_FERNET_KEY"]


@pytest.mark.helm
def test_the_default_kms_backend_is_one_we_can_configure(helm_available, chart_dir):
    """APP_KMS_BACKEND must name a backend the chart actually wires an env for."""
    if not helm_available:
        pytest.skip("helm not installed")

    docs = _render(chart_dir)
    backend = None
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "APP_KMS_BACKEND" in (
            doc.get("data") or {}
        ):
            backend = doc["data"]["APP_KMS_BACKEND"]
            break

    assert backend in BACKEND_ENV, (
        f"APP_KMS_BACKEND={backend!r} is not a backend this chart knows how to "
        f"give a key to (known: {sorted(BACKEND_ENV)})"
    )


# A third test -- "switching kms.backend to a cloud backend still leaves the pod
# able to encrypt" -- was written and REMOVED, because it failed for a real
# reason that is not this change to fix: the chart wires a key env only for the
# fernet backend. aws_kms/gcp_kms read AWS_KMS_KEY_ID / GCP_KMS_KEY_NAME from
# the ConfigMap, both defaulting to "", and vault_transit/azure_kv get nothing
# at all. Setting kms.backend to one of those today produces a pod whose
# get_backend() cannot encrypt. Filed as #1290 rather than asserted here, so
# this file keeps testing the default deployment instead of failing red on a
# pre-existing chart gap.
