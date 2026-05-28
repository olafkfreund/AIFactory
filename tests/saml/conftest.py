"""SAML test fixtures (Epic #35 #41).

Two halves:

1. **SP-side fixtures** — load the committed test SP cert + key so
   client.py can build settings dicts that round-trip through the
   OneLogin SDK without needing a real CA / production keys.

2. **IdP-side fixtures** — helpers that generate signed SAML
   `<Response>` documents we can POST at our own /saml/acs endpoint.
   This mimics what a real IdP would emit so the routes layer can be
   tested end-to-end without spinning a Keycloak container.

See ``tests/fixtures/saml/README.md`` for the security note on the
committed test keys.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER = REPO_ROOT / "apps" / "web-server"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "saml"

if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))


@pytest.fixture
def sp_cert_pem() -> str:
    return (FIXTURES_DIR / "sp-test.crt").read_text(encoding="utf-8")


@pytest.fixture
def sp_key_pem() -> str:
    return (FIXTURES_DIR / "sp-test.key").read_text(encoding="utf-8")


@pytest.fixture
def idp_cert_pem() -> str:
    return (FIXTURES_DIR / "idp-test.crt").read_text(encoding="utf-8")


@pytest.fixture
def idp_key_pem() -> str:
    return (FIXTURES_DIR / "idp-test.key").read_text(encoding="utf-8")


@pytest.fixture
def idp_metadata_xml(idp_cert_pem) -> str:
    """A minimal IdP metadata XML good enough for OneLogin's parser.

    Real IdPs ship far richer metadata (org descriptors, contact info,
    multiple keys); we omit everything optional so the test fixture
    isn't a vendor-specific blob."""
    # Strip PEM wrappers — IdP metadata embeds the raw base64.
    cert_b64 = "".join(
        line for line in idp_cert_pem.splitlines()
        if line.strip() and not line.startswith("-----")
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
                  entityID="https://test-idp.example.com/saml">
  <IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <KeyDescriptor use="signing">
      <KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">
        <X509Data>
          <X509Certificate>{cert_b64}</X509Certificate>
        </X509Data>
      </KeyInfo>
    </KeyDescriptor>
    <SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="https://test-idp.example.com/saml/sso"/>
    <SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        Location="https://test-idp.example.com/saml/sso"/>
  </IDPSSODescriptor>
</EntityDescriptor>'''


@pytest.fixture
def base_sp_config(sp_cert_pem, sp_key_pem, idp_metadata_xml, tmp_path):
    """A SamlSpConfig pointing at a file-based metadata source.

    URL-sourced tests pick this up and swap `idp_metadata_file` for
    `idp_metadata_url` in their own scope."""
    from server.saml.client import SamlSpConfig

    metadata_file = tmp_path / "idp-metadata.xml"
    metadata_file.write_text(idp_metadata_xml, encoding="utf-8")

    return SamlSpConfig(
        enabled=True,
        sp_entity_id="https://test-sp.example.com/saml",
        acs_url="https://test-sp.example.com/saml/acs",
        idp_metadata_url=None,
        idp_metadata_file=str(metadata_file),
        sp_cert_pem=sp_cert_pem,
        sp_key_pem=sp_key_pem,
        sp_cert_previous_pem=None,
        require_encrypted_assertion=False,
        idp_init_default_return_to="https://test-sp.example.com/",
    )
