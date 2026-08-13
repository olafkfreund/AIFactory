"""The outbound-URL guard, in both postures.

The point of the strict/permissive split is that the fleet runs a self-hosted
Ollama on a private address. A guard that blocks RFC-1918 outright is not
"more secure" here -- it breaks the product, gets reverted, and leaves nothing.
So the permissive posture keeps only the guards that cost no legitimate use.
"""

from __future__ import annotations

import ipaddress
from unittest.mock import patch

import pytest
from server.routes import git, llm_endpoints, settings
from server.services.url_safety import assert_safe_outbound_url

# 169.254.169.254 is the cloud metadata address. Both postures must refuse it;
# that is the whole reason the permissive posture is not simply "no check".
METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"

# The IPv6 half of the same service. It is UNIQUE-LOCAL (fc00::/7), not
# link-local, which is what makes it the interesting one -- see below.
METADATA_V6 = "http://[fd00:ec2::254]/latest/meta-data/"


@pytest.mark.parametrize("url", [METADATA, METADATA_V6])
@pytest.mark.parametrize("allow_private", [False, True])
def test_metadata_is_refused_in_both_postures(url: str, allow_private: bool) -> None:
    with pytest.raises(ValueError, match="link-local/metadata"):
        assert_safe_outbound_url(url, allow_private=allow_private)


def test_the_replaced_copies_caught_ipv6_metadata_only_by_accident() -> None:
    """Why the two copies removed in #1265 had to go, stated precisely.

    They did block ``fd00:ec2::254`` -- but only via ``ip.is_private``, which is
    incidental: it is the same clause that blocks a LAN printer. Neither of the
    two named conditions people reach for catches it.

    That mattered because the copies had no permissive posture and the canonical
    grew one. The moment anyone had added ``allow_private`` to a copy -- the
    obvious next request, since self-hosted LLM servers live on private
    addresses -- the only thing standing between a user-supplied URL and IPv6
    instance credentials would have vanished, silently, with no test to notice.
    The canonical refuses it by name in BOTH postures (the test above), so that
    edit is no longer possible to get wrong.
    """
    ip = ipaddress.ip_address("fd00:ec2::254")
    assert ip.is_private, "the copies' only reason to refuse it"
    assert not ip.is_link_local, "so an is_link_local check would let it through"
    assert not ip.is_reserved, "and so would is_reserved"


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


def test_the_guard_returns_the_url_so_a_call_site_cannot_check_and_forget() -> None:
    """The return value is load-bearing, not decoration.

    Every route below fetches what the guard RETURNED. If this ever goes back to
    returning ``None``, those call sites break loudly instead of quietly
    reverting to fetching the unchecked string -- and the CodeQL barrier in
    .github/codeql/custom-queries/SsrfBarriers.qll, which is registered on this
    call, stops clearing anything.
    """
    url = "http://10.0.0.5:11434/api/tags"
    assert assert_safe_outbound_url(url, allow_private=True) == url


# ---------------------------------------------------------------------------
# Route-level wiring.
#
# The unit tests above prove the guard is correct. These prove it is CALLED --
# the failure that actually happened, where url_safety.py landed wired into a
# single endpoint and twelve other outbound fetches kept their own unchecked
# path to the network.
#
# Each one asserts the transport was never touched, not merely that an error
# came back: "returns an error" is also what an unguarded route does when it
# fails to reach 169.254.169.254, so only "never dialled" distinguishes them.
# ---------------------------------------------------------------------------

METADATA_BASE = "http://169.254.169.254"

# Asserting the transport was never CONSTRUCTED, rather than making the mock
# raise: every one of these routes wraps its fetch in `except Exception`, which
# swallows an AssertionError just as happily as a connection error, so a raising
# mock would pass whether or not the guard fired.


def test_ollama_status_probe_refuses_the_metadata_address() -> None:
    with patch.object(git, "build_no_redirect_opener") as opener:
        assert git.check_ollama_running(METADATA_BASE) is False
    opener.assert_not_called()


def test_ollama_status_probe_still_reaches_a_self_hosted_host() -> None:
    """The regression that matters commercially: private Ollama must still work."""
    with patch.object(git, "build_no_redirect_opener") as opener:
        assert git.check_ollama_running("http://10.0.0.5:11434") is True
    opener.return_value.open.assert_called_once()


async def test_git_ollama_model_routes_refuse_the_metadata_address() -> None:
    with patch.object(git, "build_no_redirect_opener") as opener:
        assert (await git.list_ollama_models(METADATA_BASE))["data"] == []
        embedding = await git.list_ollama_embedding_models(METADATA_BASE)
        assert embedding["data"]["embedding_models"] == []
        pull = await git.pull_ollama_model(
            git.PullModelRequest(modelName="x", baseUrl=METADATA_BASE)
        )
    assert pull["success"] is False
    assert "link-local/metadata" in pull["error"]
    opener.assert_not_called()


async def test_mcp_health_refuses_a_non_http_scheme() -> None:
    with patch.object(git, "build_no_redirect_opener") as opener:
        result = await git.check_mcp_health(
            git.McpServerConfig(
                id="s1", name="s", type="http", url="file:///etc/passwd"
            )
        )
    assert result["data"]["status"] == "unhealthy"
    assert "refused to probe this URL" in result["data"]["message"]
    opener.assert_not_called()


async def test_settings_local_provider_routes_refuse_the_metadata_address() -> None:
    """Ollama / OpenAI-compatible probes: permissive posture, metadata still out."""
    with patch("httpx.AsyncClient") as client:
        results = [
            await settings.list_ollama_models(METADATA_BASE),
            await settings.list_openai_compat_models(METADATA_BASE),
            await settings.test_openai_compat_connection(
                settings.OpenAICompatTestRequest(baseUrl=METADATA_BASE)
            ),
            await settings.pull_ollama_model("m", METADATA_BASE),
            await settings.test_ollama_connection(METADATA_BASE, "m"),
        ]
    for result in results:
        assert result["success"] is False
        assert "link-local/metadata" in result["error"]
    client.assert_not_called()


async def test_settings_local_provider_routes_still_allow_a_private_host() -> None:
    """A private address must pass the guard and reach the transport."""
    with patch("httpx.AsyncClient") as client:
        await settings.list_ollama_models("http://10.0.0.5:11434")
    client.assert_called_once()


async def test_api_profile_probes_use_the_strict_posture() -> None:
    """API profiles are CLOUD endpoints and carry the caller's bearer token, so
    a private target is refused outright -- unlike the self-hosted routes."""
    request = settings.TestConnectionRequest(
        baseUrl="http://127.0.0.1:9999", apiKey="sk-secret"
    )
    with patch.object(settings, "build_no_redirect_opener") as opener:
        test = await settings.test_api_connection(request)
        discover = await settings.discover_api_models(request)
    assert "non-public" in test["error"]
    assert "non-public" in discover["error"]
    opener.assert_not_called()


# ---------------------------------------------------------------------------
# llm-endpoints /test: the PERMISSIVE posture, decided in #1268.
#
# The module's own docstring advertises LM Studio and vLLM, which live on
# localhost. Under the strict posture two of its three advertised targets were
# unreachable. These two tests are the pair that keeps the decision honest: the
# self-hosted case must reach the transport, and the metadata address must
# still not, in the same posture.
# ---------------------------------------------------------------------------

LM_STUDIO = "http://127.0.0.1:1234"


def test_llm_endpoint_probe_reaches_a_self_hosted_server() -> None:
    """#1268: LM Studio on localhost must get as far as the network.

    Asserts the transport was CONSTRUCTED and dialled with the right URL, not
    merely that no error came back: "returns an error" is also what an
    unguarded route does when it cannot connect, so only "was it dialled"
    separates a permitted URL from a refused one.
    """
    with patch.object(llm_endpoints, "build_no_redirect_opener") as opener:
        resp = opener.return_value.open.return_value.__enter__.return_value
        resp.getcode.return_value = 200
        resp.read.return_value = b'{"data": [{"id": "qwen3-coder"}]}'
        result = llm_endpoints._probe_models(LM_STUDIO, None, None)
    opener.return_value.open.assert_called_once()
    request = opener.return_value.open.call_args.args[0]
    assert request.full_url == f"{LM_STUDIO}/v1/models"
    assert result.ok is True
    assert result.models == ["qwen3-coder"]


def test_llm_endpoint_probe_still_refuses_the_metadata_address() -> None:
    """The bound on the permissive posture: same route, same posture."""
    with patch.object(llm_endpoints, "build_no_redirect_opener") as opener:
        result = llm_endpoints._probe_models(METADATA_BASE, None, None)
    assert result.ok is False
    assert "link-local/metadata" in (result.error or "")
    opener.assert_not_called()
