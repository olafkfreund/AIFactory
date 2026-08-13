"""The app pod must always receive a KMS key (#1276, #1290).

This test exists to hold up an assumption made somewhere else. #1276 encrypts
the JSON credential stores (claude-profiles.json, api-profiles.json,
settings.json) with the repo's existing crypto.kms backend -- but
``secret_field.seal()`` deliberately DEGRADES to writing plaintext, with a
one-time warning, when no backend was selected. An operator with no key must
still be able to save a profile, and is no worse off than before that change.

Which means "the credential stores are encrypted at rest" is only true while a
key actually reaches the pod. Today the chart wires ``KMS_FERNET_KEY`` from the
``aifactory-kms`` Secret by default, which is why it holds -- but a chart
default is exactly the kind of thing that gets changed in a values file two
quarters from now, and the failure mode is silent: tokens go back to plaintext
and only a log line says so. A comment cannot catch that. This can.

#1290 extended the same guarantee to the four cloud backends: selecting one
without its key now fails ``helm template`` instead of rendering a pod that
cannot encrypt. The runtime half of that fix lives in
``crypto.kms.enforce_kms_safety`` -- the chart cannot see an empty Secret or an
unreachable KMS.

If you are here because this test failed, the fix is to restore the key wiring,
not to weaken the assertion.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

# What crypto.kms.<backend>.from_env() reads before it can encrypt. Names come
# from the backend modules, not from the chart -- a test that copies the
# chart's spelling passes when both are wrong together.
BACKEND_REQUIRED_ENV = {
    "fernet": ["KMS_FERNET_KEY"],
    "aws_kms": ["AWS_KMS_KEY_ID"],
    "vault_transit": ["VAULT_ADDR", "VAULT_TOKEN", "VAULT_TRANSIT_KEY"],
    "azure_kv": ["AZURE_KEYVAULT_URL", "AZURE_KEYVAULT_KEY"],
    "gcp_kms": ["GCP_KMS_KEY_NAME"],
}

# The minimum an operator must supply to select each backend.
BACKEND_VALUES = {
    "fernet": [],
    "aws_kms": ["kms.awsKmsKeyId=arn:aws:kms:eu-west-1:111122223333:key/abcd"],
    "vault_transit": [
        "kms.vaultAddr=https://vault.vault.svc:8200",
        "kms.vaultTokenRef.name=aifactory-kms",
    ],
    "azure_kv": [
        "kms.azureKeyvaultUrl=https://kv-aifactory.vault.azure.net",
        "kms.azureKeyvaultKey=aifactory-root",
    ],
    "gcp_kms": ["kms.gcpKmsKeyName=projects/p/locations/l/keyRings/r/cryptoKeys/k"],
}

CLOUD_BACKENDS = sorted(set(BACKEND_VALUES) - {"fernet"})


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


def _reachable_env(docs: list[dict]) -> dict[str, object]:
    """Every env name the app process will see, resolved like the kubelet does.

    Union of the container's own ``env`` entries and the keys of every
    ConfigMap pulled in via ``envFrom``. Asserting only on ``env`` would be
    testing one wiring MECHANISM rather than the PROPERTY that the value
    arrives at all -- the mistake that sent the first draft of the cloud-backend
    test red for a reason unrelated to what it was meant to protect.
    """
    configmaps = {
        doc["metadata"]["name"]: (doc.get("data") or {})
        for doc in docs
        if doc.get("kind") == "ConfigMap"
    }
    container = _app_container(docs)
    resolved: dict[str, object] = {}
    for source in container.get("envFrom") or []:
        ref = source.get("configMapRef") or {}
        resolved.update(configmaps.get(ref.get("name"), {}))
    for entry in container.get("env") or []:
        resolved[entry["name"]] = entry
    return resolved


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

    assert backend in BACKEND_REQUIRED_ENV, (
        f"APP_KMS_BACKEND={backend!r} is not a backend this chart knows how to "
        f"give a key to (known: {sorted(BACKEND_REQUIRED_ENV)})"
    )


@pytest.mark.helm
@pytest.mark.parametrize("backend", sorted(BACKEND_REQUIRED_ENV))
def test_every_selectable_backend_leaves_the_pod_able_to_encrypt(
    helm_available, chart_dir, backend
):
    """The third test from #1288, restored: selecting any advertised backend
    must leave the pod able to encrypt -- the property, not the wiring."""
    if not helm_available:
        pytest.skip("helm not installed")

    docs = _render(chart_dir, [f"kms.backend={backend}", *BACKEND_VALUES[backend]])
    env = _reachable_env(docs)

    missing = [name for name in BACKEND_REQUIRED_ENV[backend] if not env.get(name)]
    assert not missing, (
        f"kms.backend={backend} renders a pod missing {missing} -- "
        "crypto.kms.get_backend() cannot encrypt, and the JSON credential "
        "stores fall back to plaintext. See #1290."
    )


@pytest.mark.helm
@pytest.mark.parametrize("backend", CLOUD_BACKENDS)
def test_selecting_a_backend_without_its_key_refuses_to_render(
    helm_available, chart_dir, backend
):
    """The other half of the property: no silent half-configured deployment.

    A chart that happily renders a keyless cloud backend IS #1290, so the gate
    is only real if it goes red here.
    """
    if not helm_available:
        pytest.skip("helm not installed")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _render(chart_dir, [f"kms.backend={backend}"])
    assert f"kms.backend={backend} requires" in excinfo.value.stderr


@pytest.mark.helm
def test_the_vault_token_is_a_secret_ref_not_a_values_literal(
    helm_available, chart_dir
):
    """The Vault token is a real credential -- Secret ref only, never values."""
    if not helm_available:
        pytest.skip("helm not installed")

    docs = _render(
        chart_dir, ["kms.backend=vault_transit", *BACKEND_VALUES["vault_transit"]]
    )
    token = _reachable_env(docs)["VAULT_TOKEN"]
    assert isinstance(token, dict)
    assert "secretKeyRef" in token.get("valueFrom", {})
    assert "value" not in token
