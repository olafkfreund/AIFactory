"""Helm chart acceptance tests for the SAML + SCIM toggles.

Epic #35 #41 PR-2. Mirrors the shape of test_otel_toggle.py +
test_audit_anchor_toggle.py.

When ``saml.enabled=false`` AND ``scim.enabled=false`` (defaults):
  * No SAML_* env vars on the web container.
  * No SCIM_BEARER_TOKEN env var on the web container.
  * No saml-idp-metadata / saml-sp-cert volumes mounted.
  * Chart renders byte-for-byte the same as pre-#41 deployments.

When ``saml.enabled=true`` with the required fields:
  * SAML_ENABLED + SAML_SP_ENTITY_ID + SAML_ACS_URL + SAML_REQUIRE_ENCRYPTED_ASSERTION
    are always present.
  * SAML_IDP_METADATA_URL XOR SAML_IDP_METADATA_FILE depending on which
    metadata source the operator picked.
  * SAML_SP_CERT_FILE + SAML_SP_KEY_FILE + SAML_SP_CERT_PREVIOUS_FILE
    are emitted when ``spCertSecretName`` is set; the optional
    ``cert.pem.previous`` key projects via a separate volume source
    marked ``optional: true`` so steady-state operation (no rotation
    in flight) doesn't require the key to exist.
  * SAML_IDP_NAME + SAML_IDP_DISPLAY_NAME + SAML_IDP_INIT_DEFAULT_RETURN_TO
    are emitted only when set (so operators can leave them empty for
    the v1.1 default-single-IdP shape).

When ``scim.enabled=true`` with ``tokenSecretName``:
  * SCIM_BEARER_TOKEN env var is wired via valueFrom.secretKeyRef.

Validators (each fails at ``helm template`` time, not at first pod
start — operator sees the misconfig immediately at ``helm install``):
  * ``saml.enabled=true`` with neither idpMetadataUrl nor
    idpMetadataSecretName.
  * ``saml.enabled=true`` with BOTH idpMetadataUrl AND
    idpMetadataSecretName (mutually exclusive).
  * ``saml.requireEncryptedAssertion=true`` without ``saml.enabled=true``.
  * ``saml.requireEncryptedAssertion=true`` without ``spCertSecretName``.
  * ``scim.enabled=true`` without ``scim.tokenSecretName``.

Sanity: SAML + SCIM toggles coexist with the other v1.1 toggles
(Redis, OTel, audit anchor) without breaking their env wiring.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        f"expected helm template to fail; got rc=0 stdout={out.stdout[:400]}"
    )
    return out.stderr


def _find_deployment(docs: list[dict]) -> dict:
    for d in docs:
        if d.get("kind") == "Deployment":
            return d
    raise AssertionError("no Deployment in rendered chart")


def _container(docs: list[dict]) -> dict:
    return _find_deployment(docs)["spec"]["template"]["spec"]["containers"][0]


def _env_names(docs: list[dict]) -> list[str]:
    return [e["name"] for e in _container(docs).get("env", [])]


def _env(docs: list[dict], name: str) -> dict:
    for e in _container(docs).get("env", []):
        if e["name"] == name:
            return e
    raise AssertionError(f"env var {name} not present")


def _volume_names(docs: list[dict]) -> list[str]:
    pod = _find_deployment(docs)["spec"]["template"]["spec"]
    return [v["name"] for v in pod.get("volumes", [])]


# ---------------------------------------------------------------------------
# Default: SAML + SCIM off — no leaks
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestSamlScimOff:
    """Default — no SAML/SCIM envs or volumes on the container, chart
    byte-for-byte same as pre-#41 deployments."""

    def test_no_saml_env_vars(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        names = _env_names(docs)
        for n in (
            "SAML_ENABLED",
            "SAML_SP_ENTITY_ID",
            "SAML_ACS_URL",
            "SAML_IDP_METADATA_URL",
            "SAML_IDP_METADATA_FILE",
            "SAML_SP_CERT_FILE",
            "SAML_SP_KEY_FILE",
            "SAML_SP_CERT_PREVIOUS_FILE",
            "SAML_REQUIRE_ENCRYPTED_ASSERTION",
            "SAML_IDP_NAME",
            "SAML_IDP_DISPLAY_NAME",
            "SAML_IDP_INIT_DEFAULT_RETURN_TO",
        ):
            assert n not in names, (
                f"{n} leaked into container env when saml.enabled=false"
            )

    def test_no_scim_env_vars(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        assert "SCIM_BEARER_TOKEN" not in _env_names(docs)

    def test_no_saml_volumes(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        vols = _volume_names(docs)
        assert "saml-idp-metadata" not in vols
        assert "saml-sp-cert" not in vols


# ---------------------------------------------------------------------------
# SAML on with URL-sourced IdP metadata + no SP cert
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestSamlOnUrlMetadata:
    """Minimal SAML happy path: URL-sourced metadata, no SP cert
    (operator's IdP doesn't require encrypted assertions)."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://aifactory.example.com/saml",
                "saml.acsUrl=https://aifactory.example.com/api/auth/saml/acs",
                "saml.idpMetadataUrl=https://idp.example.com/saml/metadata",
            ],
        )

    def test_enabled_flag(self, helm_available, chart_dir) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_ENABLED")
        assert e["value"] == "true"

    def test_sp_entity_id(self, helm_available, chart_dir) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_SP_ENTITY_ID")
        assert e["value"] == "https://aifactory.example.com/saml"

    def test_acs_url(self, helm_available, chart_dir) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_ACS_URL")
        assert e["value"] == "https://aifactory.example.com/api/auth/saml/acs"

    def test_idp_metadata_url(self, helm_available, chart_dir) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_IDP_METADATA_URL")
        assert e["value"] == "https://idp.example.com/saml/metadata"

    def test_no_metadata_file_env(self, helm_available, chart_dir) -> None:
        """URL-sourced ⇒ no FILE env; mutually exclusive."""
        assert "SAML_IDP_METADATA_FILE" not in _env_names(self._docs_on(chart_dir))

    def test_no_sp_cert_envs(self, helm_available, chart_dir) -> None:
        """No spCertSecretName ⇒ no SP_CERT/KEY/PREVIOUS env vars."""
        names = _env_names(self._docs_on(chart_dir))
        for n in (
            "SAML_SP_CERT_FILE",
            "SAML_SP_KEY_FILE",
            "SAML_SP_CERT_PREVIOUS_FILE",
        ):
            assert n not in names

    def test_require_encrypted_default_false(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_REQUIRE_ENCRYPTED_ASSERTION")
        assert e["value"] == "false"

    def test_optional_idp_name_envs_absent_when_unset(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        names = _env_names(self._docs_on(chart_dir))
        for n in (
            "SAML_IDP_NAME",
            "SAML_IDP_DISPLAY_NAME",
            "SAML_IDP_INIT_DEFAULT_RETURN_TO",
        ):
            assert n not in names


# ---------------------------------------------------------------------------
# SAML on with Secret-sourced IdP metadata + SP cert
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestSamlOnSecretMetadataAndSpCert:
    """Air-gapped happy path: Secret-mounted metadata + SP cert for
    AuthnRequest signing + (optionally) decryption."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://aifactory.example.com/saml",
                "saml.acsUrl=https://aifactory.example.com/api/auth/saml/acs",
                "saml.idpMetadataSecretName=aifactory-saml-idp",
                "saml.spCertSecretName=aifactory-saml-sp",
                "saml.idpName=corp-sso",
                "saml.idpDisplayName=Corp SSO (SAML)",
                "saml.idpInitDefaultReturnTo=https://aifactory.example.com/",
            ],
        )

    def test_metadata_file_env_points_at_mount(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_IDP_METADATA_FILE")
        assert e["value"] == "/etc/saml/idp-metadata.xml"

    def test_no_metadata_url_env(self, helm_available, chart_dir) -> None:
        assert "SAML_IDP_METADATA_URL" not in _env_names(self._docs_on(chart_dir))

    def test_sp_cert_env_paths(self, helm_available, chart_dir) -> None:
        docs = self._docs_on(chart_dir)
        cert = _env(docs, "SAML_SP_CERT_FILE")
        key = _env(docs, "SAML_SP_KEY_FILE")
        prev = _env(docs, "SAML_SP_CERT_PREVIOUS_FILE")
        assert cert["value"] == "/etc/saml/sp/cert.pem"
        assert key["value"] == "/etc/saml/sp/key.pem"
        assert prev["value"] == "/etc/saml/sp/cert.pem.previous"

    def test_idp_metadata_volume_mounted(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        docs = self._docs_on(chart_dir)
        assert "saml-idp-metadata" in _volume_names(docs)
        mounts = _container(docs).get("volumeMounts", [])
        mount = next(m for m in mounts if m["name"] == "saml-idp-metadata")
        assert mount["mountPath"] == "/etc/saml/idp-metadata.xml"
        assert mount["subPath"] == "idp-metadata.xml"
        assert mount["readOnly"] is True

    def test_sp_cert_volume_mounted_as_dir(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """SP cert mounts as a directory (no subPath) so the optional
        cert.pem.previous file shows up alongside cert.pem + key.pem
        without needing per-file mount entries."""
        docs = self._docs_on(chart_dir)
        assert "saml-sp-cert" in _volume_names(docs)
        mounts = _container(docs).get("volumeMounts", [])
        mount = next(m for m in mounts if m["name"] == "saml-sp-cert")
        assert mount["mountPath"] == "/etc/saml/sp"
        assert mount.get("subPath") is None
        assert mount["readOnly"] is True

    def test_sp_cert_volume_projects_previous_as_optional(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """The rotation-overlap key (cert.pem.previous) must come from
        an ``optional: true`` source so steady-state operation doesn't
        require it to exist in the Secret."""
        docs = self._docs_on(chart_dir)
        pod = _find_deployment(docs)["spec"]["template"]["spec"]
        vol = next(v for v in pod["volumes"] if v["name"] == "saml-sp-cert")
        # Projected volume with TWO secret sources: required cert+key,
        # optional cert.pem.previous.
        sources = vol["projected"]["sources"]
        assert len(sources) == 2
        required = sources[0]["secret"]
        optional = sources[1]["secret"]
        assert "optional" not in required or required["optional"] is False
        assert optional["optional"] is True
        # The optional source projects ONLY the previous-cert key.
        optional_keys = {item["key"] for item in optional["items"]}
        assert optional_keys == {"cert.pem.previous"}

    def test_idp_name_env(self, helm_available, chart_dir) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_IDP_NAME")
        assert e["value"] == "corp-sso"

    def test_idp_display_name_env(self, helm_available, chart_dir) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_IDP_DISPLAY_NAME")
        assert e["value"] == "Corp SSO (SAML)"

    def test_idp_init_default_return_to_env(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        e = _env(self._docs_on(chart_dir), "SAML_IDP_INIT_DEFAULT_RETURN_TO")
        assert e["value"] == "https://aifactory.example.com/"


# ---------------------------------------------------------------------------
# SCIM on
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestScimOn:
    """SCIM Bearer token wired from K8s Secret per design decision #3."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "scim.enabled=true",
                "scim.tokenSecretName=aifactory-scim",
            ],
        )

    def test_bearer_token_env_from_secret(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        e = _env(self._docs_on(chart_dir), "SCIM_BEARER_TOKEN")
        assert "value" not in e
        ref = e["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "aifactory-scim"
        assert ref["key"] == "SCIM_BEARER_TOKEN"

    def test_no_saml_envs_when_only_scim_on(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """SCIM is independent of SAML — turning one on doesn't drag
        the other in."""
        names = _env_names(self._docs_on(chart_dir))
        assert "SAML_ENABLED" not in names


# ---------------------------------------------------------------------------
# Validators (4 from design §8)
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestSamlScimValidators:
    """Each validator from design §8 fires at ``helm template`` time
    with a clear, operator-actionable error message."""

    def test_saml_enabled_without_metadata_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://x.example.com/saml",
                "saml.acsUrl=https://x.example.com/api/auth/saml/acs",
                # both metadata fields left empty
            ],
        )
        assert (
            "saml.enabled=true requires exactly one of "
            "saml.idpMetadataUrl OR saml.idpMetadataSecretName"
        ) in stderr, f"got stderr={stderr[:600]}"

    def test_saml_enabled_with_both_metadata_sources_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://x.example.com/saml",
                "saml.acsUrl=https://x.example.com/api/auth/saml/acs",
                "saml.idpMetadataUrl=https://idp/m",
                "saml.idpMetadataSecretName=idp-meta",
            ],
        )
        assert (
            "saml.idpMetadataUrl and saml.idpMetadataSecretName are mutually exclusive"
        ) in stderr, f"got stderr={stderr[:600]}"

    def test_saml_enabled_without_sp_entity_id_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                # spEntityId missing
                "saml.acsUrl=https://x.example.com/api/auth/saml/acs",
                "saml.idpMetadataUrl=https://idp/m",
            ],
        )
        assert "saml.enabled=true requires saml.spEntityId" in stderr, (
            f"got stderr={stderr[:600]}"
        )

    def test_saml_enabled_without_acs_url_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://x.example.com/saml",
                # acsUrl missing
                "saml.idpMetadataUrl=https://idp/m",
            ],
        )
        assert "saml.enabled=true requires saml.acsUrl" in stderr, (
            f"got stderr={stderr[:600]}"
        )

    def test_require_encrypted_without_enabled_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """Operator typo trap: encryption requirement is meaningless
        without SAML being on at all. Fail loud at template time."""
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.requireEncryptedAssertion=true",
                # saml.enabled left as default false
            ],
        )
        assert (
            "saml.requireEncryptedAssertion=true requires saml.enabled=true"
        ) in stderr, f"got stderr={stderr[:600]}"

    def test_require_encrypted_without_sp_cert_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        """Encryption needs the SP private key. Without spCertSecretName
        the SDK can't decrypt; fail at template time."""
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://x.example.com/saml",
                "saml.acsUrl=https://x.example.com/api/auth/saml/acs",
                "saml.idpMetadataUrl=https://idp/m",
                "saml.requireEncryptedAssertion=true",
                # spCertSecretName missing
            ],
        )
        assert (
            "saml.requireEncryptedAssertion=true requires saml.spCertSecretName"
        ) in stderr, f"got stderr={stderr[:600]}"

    def test_scim_enabled_without_token_secret_fails(
        self,
        helm_available,
        chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "scim.enabled=true",
                # tokenSecretName missing
            ],
        )
        assert "scim.enabled=true requires scim.tokenSecretName" in stderr, (
            f"got stderr={stderr[:600]}"
        )


# ---------------------------------------------------------------------------
# Coexistence with other v1.1 toggles
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestSamlScimCoexistsWithOtherToggles:
    """SAML + SCIM alongside Redis + OTel + audit anchor doesn't break
    any of their env wiring."""

    def test_all_envs_render(self, helm_available, chart_dir) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "saml.enabled=true",
                "saml.spEntityId=https://x.example.com/saml",
                "saml.acsUrl=https://x.example.com/api/auth/saml/acs",
                "saml.idpMetadataUrl=https://idp/m",
                "scim.enabled=true",
                "scim.tokenSecretName=aifactory-scim",
                "redis.enabled=true",
                "redis.url=redis://r:6379/0",
                "otel.enabled=true",
                "otel.endpoint=http://tempo:4317",
                "audit.anchor.enabled=true",
            ],
        )
        names = _env_names(docs)
        for n in (
            "SAML_ENABLED",
            "SAML_SP_ENTITY_ID",
            "SCIM_BEARER_TOKEN",
            "REDIS_URL",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "AUDIT_ANCHOR_ENABLED",
        ):
            assert n in names, f"{n} missing when v1.1 toggles coexist"
