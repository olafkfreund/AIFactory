"""Tests for the SamlClient wrapper (Epic #35 #41).

Covers:
- ``initialise()`` loads file-sourced metadata + returns valid IdP
  dict to ``build_settings_dict``.
- The hard-coded security knobs make it into the settings dict:
  ``strict=True``, ``wantAssertionsSigned=True``,
  ``NameIDFormat=emailAddress``, RSA-SHA256.
- ``require_encrypted_assertion`` flag passes through to the SDK.
- SP cert rotation: when ``sp_cert_previous_pem`` is set, the
  settings dict has ``x509certMulti`` with both certs in the
  encryption list (current + previous in order).
- ``shutdown()`` is safe to call without prior ``initialise()``.
- The settings dict actually constructs a valid OneLogin Saml2_Settings
  object (catches typos in our dict shape).
"""

from __future__ import annotations

import time

import pytest


def test_initialise_loads_file_metadata(base_sp_config):
    from server.saml.client import SamlClient

    client = SamlClient(base_sp_config)
    client.initialise()
    age = client.metadata_age_seconds()
    assert age is not None
    assert age < 5  # loaded essentially just now


def test_settings_dict_includes_security_constants(base_sp_config):
    from server.saml.client import SamlClient

    client = SamlClient(base_sp_config)
    client.initialise()
    settings = client.build_settings_dict()

    # XSW defence — never configurable.
    assert settings["strict"] is True

    # Inner-Assertion signing requirement.
    assert settings["security"]["wantAssertionsSigned"] is True

    # The NameID format is locked to email per decision #6.
    assert (
        settings["sp"]["NameIDFormat"]
        == "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
    )

    # Crypto choices: RSA-SHA256 / SHA-256 (not the deprecated SHA-1).
    assert "rsa-sha256" in settings["security"]["signatureAlgorithm"]
    assert "sha256" in settings["security"]["digestAlgorithm"]


def test_require_encrypted_assertion_passes_through(base_sp_config):
    from server.saml.client import SamlClient

    base_sp_config.require_encrypted_assertion = True
    client = SamlClient(base_sp_config)
    client.initialise()
    settings = client.build_settings_dict()
    assert settings["security"]["wantAssertionsEncrypted"] is True


def test_default_encryption_off(base_sp_config):
    from server.saml.client import SamlClient

    client = SamlClient(base_sp_config)
    client.initialise()
    settings = client.build_settings_dict()
    assert settings["security"]["wantAssertionsEncrypted"] is False


def test_sp_cert_rotation_overlap(base_sp_config, sp_cert_pem):
    """When sp_cert_previous_pem is set, x509certMulti has both certs
    in the encryption list (current first, previous after)."""
    from server.saml.client import SamlClient

    base_sp_config.sp_cert_previous_pem = sp_cert_pem  # use same for test
    client = SamlClient(base_sp_config)
    client.initialise()
    settings = client.build_settings_dict()

    assert "x509certMulti" in settings["sp"]
    multi = settings["sp"]["x509certMulti"]
    assert "encryption" in multi
    assert len(multi["encryption"]) == 2  # current + previous


def test_no_rotation_no_x509certmulti(base_sp_config):
    from server.saml.client import SamlClient

    client = SamlClient(base_sp_config)
    client.initialise()
    settings = client.build_settings_dict()
    assert "x509certMulti" not in settings["sp"]


def test_shutdown_safe_without_init(base_sp_config):
    from server.saml.client import SamlClient

    client = SamlClient(base_sp_config)
    # Don't call initialise()
    client.shutdown()  # must not raise


def test_settings_dict_constructs_valid_onelogin_object(base_sp_config):
    """Round-trip: feed our dict into OneLogin's own validator. Catches
    typos in field names that our unit tests would otherwise miss."""
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    from server.saml.client import SamlClient

    client = SamlClient(base_sp_config)
    client.initialise()
    settings_dict = client.build_settings_dict()

    # custom_base_path=None means OneLogin won't try to read certs
    # from a filesystem path (we've embedded them inline).
    # sp_validation_only=False makes it validate IdP too.
    onelogin_settings = OneLogin_Saml2_Settings(
        settings=settings_dict, sp_validation_only=False
    )
    errors = onelogin_settings.get_errors()
    assert not errors, f"OneLogin rejected our settings dict: {errors}"


def test_url_source_starts_refresh_thread(
    base_sp_config,
    idp_metadata_xml,
    monkeypatch,
):
    """When idpMetadataUrl is used (not file), the refresh thread
    starts. Confirms by checking the thread exists + is daemonic."""
    from server.saml import client as client_mod
    from server.saml.client import SamlClient

    base_sp_config.idp_metadata_file = None
    base_sp_config.idp_metadata_url = "https://test-idp.example.com/saml/metadata"

    class _FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            pass

    def _fake_get(url, timeout):
        return _FakeResponse(idp_metadata_xml)

    monkeypatch.setattr(client_mod.httpx, "get", _fake_get)

    client = SamlClient(base_sp_config)
    client.initialise()

    assert client._refresh_thread is not None
    assert client._refresh_thread.daemon is True

    client.shutdown()
    # Give the thread a tick to actually exit.
    time.sleep(0.1)


def test_initialise_requires_metadata_source(base_sp_config):
    """Both file and URL unset should fail loud at startup, not later
    on first login attempt."""
    from server.saml.client import SamlClient, SamlNotConfiguredError

    base_sp_config.idp_metadata_file = None
    base_sp_config.idp_metadata_url = None
    client = SamlClient(base_sp_config)
    with pytest.raises(SamlNotConfiguredError):
        client.initialise()
