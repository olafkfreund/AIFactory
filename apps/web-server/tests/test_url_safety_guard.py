"""The outbound-URL guard, in both postures.

The point of the strict/permissive split is that the fleet runs a self-hosted
Ollama on a private address. A guard that blocks RFC-1918 outright is not
"more secure" here -- it breaks the product, gets reverted, and leaves nothing.
So the permissive posture keeps only the guards that cost no legitimate use.
"""

from __future__ import annotations

import pytest
from server.services.url_safety import assert_safe_outbound_url

# 169.254.169.254 is the cloud metadata address. Both postures must refuse it;
# that is the whole reason the permissive posture is not simply "no check".
METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


@pytest.mark.parametrize("allow_private", [False, True])
def test_metadata_is_refused_in_both_postures(allow_private: bool) -> None:
    with pytest.raises(ValueError, match="link-local/metadata"):
        assert_safe_outbound_url(METADATA, allow_private=allow_private)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://h/f"])
@pytest.mark.parametrize("allow_private", [False, True])
def test_non_http_schemes_are_refused_in_both_postures(
    url: str, allow_private: bool
) -> None:
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        assert_safe_outbound_url(url, allow_private=allow_private)


def test_strict_posture_refuses_loopback() -> None:
    with pytest.raises(ValueError, match="non-public"):
        assert_safe_outbound_url("http://127.0.0.1:8080/v1/models")


def test_permissive_posture_allows_the_self_hosted_ollama_case() -> None:
    """The regression this split exists to prevent: a local Ollama must work."""
    assert_safe_outbound_url("http://127.0.0.1:11434/api/tags", allow_private=True)
    assert_safe_outbound_url("http://10.0.0.5:11434/api/tags", allow_private=True)


def test_a_url_with_no_host_is_refused() -> None:
    with pytest.raises(ValueError, match="no host"):
        assert_safe_outbound_url("http:///nohost")


def test_an_unresolvable_host_is_refused_rather_than_attempted() -> None:
    with pytest.raises(ValueError, match="cannot resolve host"):
        assert_safe_outbound_url("http://no-such-host.invalid/x", allow_private=True)
