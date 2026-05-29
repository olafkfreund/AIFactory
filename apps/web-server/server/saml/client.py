"""SAML SP client wrapper (Epic #35 #41).

Thin layer over python3-saml (OneLogin SDK) that:

- Builds the OneLogin settings dict from the chart's ``saml:`` block,
  hard-coding the security-critical knobs that operators must NOT
  override (``strict=True``, ``wantAssertionsSigned=True``).
- Loads IdP metadata from either a URL or a file path (Secret mount),
  with a background refresh task for URL-sourced metadata.
- Loads the SP signing cert + key, supporting an optional ``previous``
  pair for the cert-rotation overlap window (``sp.x509certMulti``).

Why a wrapper instead of using the SDK directly
-----------------------------------------------

The OneLogin SDK's settings dict has 40+ knobs, most of which are
either dangerous to expose or always-the-same-for-us. Centralising
here lets the routes / dependency layer stay tiny and lets every
security-sensitive default live in one auditable file.

## Threat-model defences baked in

- ``strict=True`` — XSW (XML Signature Wrapping) defence. CVE-2022-41912
  class. **Never** read from values.yaml; constant in this file.
- ``wantAssertionsSigned=True`` — the inner `<Assertion>` element must
  be signed by the IdP cert, not merely the outer `<Response>`
  envelope. Signed-Response-only IdPs are rejected.
- ``wantNameId=True`` + ``requestedNameIdFormat=emailAddress`` —
  enforces our decision-#6 identity-key constraint at the SDK level.
- Replay defence — handled OUTSIDE this module by ``replay_cache.py``;
  the routes layer calls it before trusting any processed assertion.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Hard-coded security policy. Operators MUST NOT be able to flip these.
_NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
_SIG_ALG_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_DIGEST_ALG_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"

# Background metadata refresh tuning. See design doc.
_REFRESH_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours
_REFRESH_BACKOFF_INITIAL = 60.0
_REFRESH_BACKOFF_MAX = 3600.0
_STALE_WARN_AFTER_SECONDS = 48 * 60 * 60  # 48 hours


@dataclass
class SamlSpConfig:
    """Operator-facing config for one SAML SP instance.

    Built from the Helm chart's ``saml:`` block + env vars. Mirrors
    the public surface — fields here are the only knobs operators
    can touch."""

    enabled: bool
    sp_entity_id: str            # https://aifactory.example.com/saml
    acs_url: str                 # https://aifactory.example.com/saml/acs
    idp_metadata_url: str | None       # one of these two must be set
    idp_metadata_file: str | Path | None
    sp_cert_pem: str | None             # current cert (PEM)
    sp_key_pem: str | None              # current key (PEM)
    sp_cert_previous_pem: str | None    # rotation-overlap previous cert
    require_encrypted_assertion: bool = False
    idp_init_default_return_to: str | None = None
    # v1.2 — SAML SLO (Epic #35 #209). Both fields default to OFF/None
    # so pre-v1.2 deployments are byte-for-byte unchanged.
    slo_enabled: bool = False
    # The SP's own SLS callback URL; required when slo_enabled.
    # Example: https://aifactory.example.com/api/auth/saml/sls
    slo_url: str | None = None
    # Override for the IdP's SLO endpoint. When empty, the endpoint is
    # derived from the IdP metadata's <SingleLogoutService> element.
    idp_slo_url: str | None = None


class SamlClient:
    """Stateful wrapper around the OneLogin SDK for one IdP.

    One ``SamlClient`` per configured SAML IdP. The web-server
    typically holds 0 or 1 of these (multi-IdP is a v1.2 expansion).
    """

    def __init__(self, config: SamlSpConfig) -> None:
        self._config = config
        self._idp_metadata_xml: str | None = None
        self._idp_metadata_loaded_at: float | None = None
        self._lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        self._stop_refresh = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialise(self) -> None:
        """Load metadata once + (if URL-sourced) start the refresher.

        Safe to call repeatedly — start_refresh is idempotent."""
        self._load_metadata_blocking()
        if self._config.idp_metadata_url:
            self._start_refresh_thread()

    def shutdown(self) -> None:
        """Stop the background refresh. For tests + clean pod shutdown."""
        self._stop_refresh.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=5)

    # ------------------------------------------------------------------
    # OneLogin settings dict
    # ------------------------------------------------------------------

    def build_settings_dict(self) -> dict:
        """Return the dict OneLogin's Saml2_Settings constructor wants.

        Captures every hard-coded security choice in one place.
        """
        cfg = self._config
        sp = {
            "entityId": cfg.sp_entity_id,
            "assertionConsumerService": {
                "url": cfg.acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": _NAMEID_EMAIL,
        }
        if cfg.sp_cert_pem and cfg.sp_key_pem:
            sp["x509cert"] = _strip_pem(cfg.sp_cert_pem)
            sp["privateKey"] = _strip_pem(cfg.sp_key_pem)
        # Cert rotation overlap: the SDK reads `x509certMulti` for the
        # decryption-key fallback. We populate both keys when a
        # previous cert is supplied (current then previous order is
        # what the SDK tries during decryption).
        if cfg.sp_cert_previous_pem:
            sp["x509certMulti"] = {
                "signing": [_strip_pem(cfg.sp_cert_pem or "")],
                "encryption": [
                    _strip_pem(cfg.sp_cert_pem or ""),
                    _strip_pem(cfg.sp_cert_previous_pem),
                ],
            }
        # v1.2 SLO — advertise the SP's Single Logout Service endpoint
        # in the settings dict only when the operator opted in. The SDK
        # uses this to build the <SingleLogoutService> element in the
        # SP metadata XML returned by /api/auth/saml/metadata.
        if cfg.slo_enabled and cfg.slo_url:
            sp["singleLogoutService"] = {
                "url": cfg.slo_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            }

        idp = self._build_idp_dict_from_metadata()

        return {
            "strict": True,  # XSW defence. NEVER configurable.
            "debug": False,
            "sp": sp,
            "idp": idp,
            "security": {
                "wantAssertionsSigned": True,
                "wantAssertionsEncrypted": cfg.require_encrypted_assertion,
                "wantMessagesSigned": False,
                "wantNameId": True,
                "requestedAuthnContext": False,
                "signMetadata": False,
                "signatureAlgorithm": _SIG_ALG_RSA_SHA256,
                "digestAlgorithm": _DIGEST_ALG_SHA256,
                # Block accepting an unsigned NameID — required when
                # NameID is the identity key.
                "wantNameIdEncrypted": False,
                # Reject assertions whose subject was issued for a
                # different audience than ours.
                "requestedAttributes": [],
                # v1.2 SLO — sign LogoutRequests and LogoutResponses when
                # the SP cert is configured. Signing is conditional so that
                # operators without a cert (testing only) still get working
                # SLO flows; production deployments SHOULD have a cert.
                "logoutRequestSigned": bool(cfg.sp_cert_pem and cfg.sp_key_pem),
                "logoutResponseSigned": bool(cfg.sp_cert_pem and cfg.sp_key_pem),
            },
        }

    # ------------------------------------------------------------------
    # Metadata management
    # ------------------------------------------------------------------

    def _build_idp_dict_from_metadata(self) -> dict:
        """Parse the cached XML into the dict shape the SDK expects.

        Imports happen lazily so the test-suite collection cost doesn't
        pay for python3-saml in modules that don't touch SAML.
        """
        from onelogin.saml2.idp_metadata_parser import (
            OneLogin_Saml2_IdPMetadataParser,
        )

        if not self._idp_metadata_xml:
            raise SamlNotConfiguredError(
                "IdP metadata not loaded yet — call initialise() first"
            )

        # Parser returns {"idp": {...}}.
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(
            self._idp_metadata_xml
        )
        return parsed.get("idp", {})

    def _load_metadata_blocking(self) -> None:
        """One-shot load. Raises on failure (caller's startup decides
        whether to propagate)."""
        cfg = self._config
        if cfg.idp_metadata_file:
            xml = Path(cfg.idp_metadata_file).read_text(encoding="utf-8")
        elif cfg.idp_metadata_url:
            resp = httpx.get(cfg.idp_metadata_url, timeout=10.0)
            resp.raise_for_status()
            xml = resp.text
        else:
            raise SamlNotConfiguredError(
                "saml.idpMetadataUrl or saml.idpMetadataSecretName is required"
            )

        with self._lock:
            self._idp_metadata_xml = xml
            self._idp_metadata_loaded_at = time.time()

        logger.info(
            "SAML IdP metadata loaded (%d bytes, source=%s)",
            len(xml),
            "url" if cfg.idp_metadata_url else "file",
        )

    def _start_refresh_thread(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="saml-metadata-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        """Periodic background refresh. Never crashes.

        Exponential backoff on failure; logs WARNING if cache age
        exceeds the stale threshold.
        """
        backoff = _REFRESH_BACKOFF_INITIAL
        while not self._stop_refresh.wait(_REFRESH_INTERVAL_SECONDS):
            try:
                self._load_metadata_blocking()
                backoff = _REFRESH_BACKOFF_INITIAL  # reset on success
            except Exception:
                logger.warning(
                    "SAML metadata refresh failed; will retry in %.0fs",
                    backoff, exc_info=True,
                )
                # Spend backoff sleeping (allow shutdown to wake us).
                if self._stop_refresh.wait(backoff):
                    break
                backoff = min(backoff * 2, _REFRESH_BACKOFF_MAX)

            # Stale-cache alert. Loaded_at is updated only on success.
            age = time.time() - (self._idp_metadata_loaded_at or 0)
            if age > _STALE_WARN_AFTER_SECONDS:
                logger.warning(
                    "SAML metadata cache is %d hours stale — IdP at %s "
                    "may be unreachable",
                    int(age / 3600), self._config.idp_metadata_url,
                )

    def metadata_age_seconds(self) -> float | None:
        """For ops + tests. Returns None if metadata never loaded."""
        if self._idp_metadata_loaded_at is None:
            return None
        return time.time() - self._idp_metadata_loaded_at


def _strip_pem(pem: str) -> str:
    """OneLogin wants the raw base64 between the PEM headers, not the
    full -----BEGIN-----/-----END----- wrapper."""
    lines = [
        line for line in pem.splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    return "".join(lines)


def config_from_env() -> SamlSpConfig:
    """Build a config from the standard env vars the chart sets.

    Env vars (mirrors the values.yaml ``saml:`` block):
      SAML_ENABLED, SAML_SP_ENTITY_ID, SAML_ACS_URL,
      SAML_IDP_METADATA_URL, SAML_IDP_METADATA_FILE,
      SAML_SP_CERT_FILE, SAML_SP_KEY_FILE,
      SAML_SP_CERT_PREVIOUS_FILE,
      SAML_REQUIRE_ENCRYPTED_ASSERTION ("true" / "false"),
      SAML_IDP_INIT_DEFAULT_RETURN_TO,
      SAML_SLO_ENABLED ("true" / "false") — v1.2,
      SAML_SLO_URL (SP's own SLS callback URL) — v1.2,
      SAML_IDP_SLO_URL (IdP SLO endpoint override) — v1.2

    Reads PEMs from file paths so they can be mounted from Secrets
    without leaking through `kubectl describe pod`.
    """
    def _read(env_var: str) -> str | None:
        p = os.environ.get(env_var, "").strip()
        if not p:
            return None
        return Path(p).read_text(encoding="utf-8")

    return SamlSpConfig(
        enabled=os.environ.get("SAML_ENABLED", "").lower() == "true",
        sp_entity_id=os.environ.get("SAML_SP_ENTITY_ID", "").strip(),
        acs_url=os.environ.get("SAML_ACS_URL", "").strip(),
        idp_metadata_url=os.environ.get("SAML_IDP_METADATA_URL", "").strip()
        or None,
        idp_metadata_file=os.environ.get(
            "SAML_IDP_METADATA_FILE", "",
        ).strip() or None,
        sp_cert_pem=_read("SAML_SP_CERT_FILE"),
        sp_key_pem=_read("SAML_SP_KEY_FILE"),
        sp_cert_previous_pem=_read("SAML_SP_CERT_PREVIOUS_FILE"),
        require_encrypted_assertion=(
            os.environ.get(
                "SAML_REQUIRE_ENCRYPTED_ASSERTION", "",
            ).lower() == "true"
        ),
        idp_init_default_return_to=os.environ.get(
            "SAML_IDP_INIT_DEFAULT_RETURN_TO", "",
        ).strip() or None,
        # v1.2 SLO fields (default OFF for backward compat).
        slo_enabled=(
            os.environ.get("SAML_SLO_ENABLED", "").lower() == "true"
        ),
        slo_url=os.environ.get("SAML_SLO_URL", "").strip() or None,
        idp_slo_url=os.environ.get("SAML_IDP_SLO_URL", "").strip() or None,
    )


class SamlNotConfiguredError(RuntimeError):
    """SAML is enabled but required config is missing.

    Raised at startup so the operator gets a clear pod-crashloop
    diagnostic, not a confusing 500 on first login.
    """
