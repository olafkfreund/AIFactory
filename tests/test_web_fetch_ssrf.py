#!/usr/bin/env python3
"""
Agent WebFetch SSRF guard (#370 — epic #318)
============================================

The agent's ``WebFetch`` tool is granted under ``bypassPermissions`` and was
not validated by any PreToolUse hook (only ``Bash`` was). These tests pin the
SSRF guard: cloud-metadata / loopback / private / link-local / non-http(s)
targets are blocked; public http(s) passes; ``WebSearch`` (query, no URL) is
untouched.

URLs use IP literals so the guard's ``getaddrinfo`` resolves without real DNS,
keeping the tests hermetic/offline.
"""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from agents.tools_pkg.tools.web import guarded_web_fetch
from security.hooks import web_fetch_security_hook
from security.url_guard import assert_url_not_ssrf, fetch_following_safe_redirects
from server.services import url_safety

# Hosts that must be refused (IP literals → no DNS needed).
SSRF_URLS = [
    "http://169.254.169.254/latest/meta-data/",  # AWS/GCP metadata (link-local)
    "http://127.0.0.1:3101/api/admin",  # loopback — local control plane
    "http://[::1]:8080/",  # loopback v6
    "http://10.0.0.5/internal",  # RFC1918 private
    "http://172.16.0.1/",  # RFC1918 private
    "http://192.168.1.10/admin",  # RFC1918 private
    "http://0.0.0.0/",  # unspecified
    "https://192.168.0.1/",  # private over https
    # IPv6 instance metadata. Unique-local, so neither is_link_local nor
    # is_reserved catches it -- the copy this guard replaced refused it only as
    # a side effect of its is_private clause (#1265).
    "http://[fd00:ec2::254]/latest/meta-data/",
]

# Non-http(s) schemes must be refused outright.
BAD_SCHEME_URLS = [
    "file:///etc/passwd",
    "gopher://127.0.0.1/",
    "ftp://10.0.0.1/",
]

# Public IP literals must pass (no DNS, no private range).
SAFE_URLS = [
    "https://1.1.1.1/",
    "http://8.8.8.8/",
]


def _run_hook(tool_name: str, tool_input) -> dict:
    return asyncio.run(
        web_fetch_security_hook({"tool_name": tool_name, "tool_input": tool_input})
    )


class TestUrlGuard:
    @pytest.mark.parametrize("url", SSRF_URLS)
    def test_ssrf_targets_raise(self, url):
        with pytest.raises(ValueError):
            assert_url_not_ssrf(url)

    @pytest.mark.parametrize("url", BAD_SCHEME_URLS)
    def test_bad_schemes_raise(self, url):
        with pytest.raises(ValueError):
            assert_url_not_ssrf(url)

    @pytest.mark.parametrize("url", SAFE_URLS)
    def test_public_urls_pass(self, url):
        # Returns the url (does not raise) for a safe URL. It used to return
        # None; the canonical guard hands the checked value back so a call site
        # cannot validate one string and then fetch another (#1264). This hook
        # ignores the return -- the SDK does the fetching -- so the change is
        # visible only here.
        assert assert_url_not_ssrf(url) == url

    def test_the_backend_guard_is_the_canonical_one_not_a_copy(self):
        """#1265: this is an identity check on purpose.

        A "same behaviour" test passes just as happily against a re-introduced
        local copy, which is exactly how the three copies drifted apart in the
        first place. This one cannot: it fails the moment `security.url_guard`
        stops being the canonical function itself.
        """
        from security import url_guard
        from server.services.url_safety import assert_safe_outbound_url

        assert url_guard.assert_url_not_ssrf is assert_safe_outbound_url


class TestWebFetchHook:
    @pytest.mark.parametrize("url", SSRF_URLS + BAD_SCHEME_URLS)
    def test_hook_blocks_unsafe(self, url):
        out = _run_hook("WebFetch", {"url": url})
        assert out.get("decision") == "block", f"NOT BLOCKED: {url}"

    @pytest.mark.parametrize("url", SAFE_URLS)
    def test_hook_allows_public(self, url):
        assert _run_hook("WebFetch", {"url": url}) == {}

    def test_websearch_query_passes(self):
        # WebSearch carries a query, not a URL — nothing to validate.
        assert _run_hook("WebSearch", {"query": "how to use ripgrep"}) == {}

    def test_other_tools_ignored(self):
        assert _run_hook("Read", {"file_path": "/etc/passwd"}) == {}

    def test_malformed_input_ignored(self):
        assert _run_hook("WebFetch", None) == {}
        assert _run_hook("WebFetch", {}) == {}


# ===========================================================================
# #1269: the redirect chain.
#
# These use a REAL http server issuing REAL 302s, because the bug being fixed
# is invisible to any test that only inspects the first URL -- and a test that
# only inspects the first URL passes just as happily against the bug. The
# dangerous URL is the one the initial check never sees.
#
# The harness runs on 127.0.0.1, so it fetches with allow_private=True. That is
# not a weakened test: the metadata range is refused in BOTH postures, so a hop
# to 169.254.169.254 through this server exercises exactly the chain from the
# issue. `test_every_hop_is_validated_with_the_same_posture` covers the private
# case, by watching what the guard was actually asked.
# ===========================================================================

METADATA_URL = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


class _RedirectHandler(BaseHTTPRequestHandler):
    """Routes named for what they do to the fetcher."""

    ROUTES = {
        "/to-metadata": ("redirect", METADATA_URL),
        "/to-private": ("redirect", "http://10.0.0.5/internal"),
        "/to-file-scheme": ("redirect", "file:///etc/passwd"),
        # Two innocent hops, then metadata. A guard that checks only the first
        # hop AND a guard that checks only the second both miss this.
        "/chain-1": ("redirect", "/chain-2"),
        "/chain-2": ("redirect", "/to-metadata"),
        # A relative Location, which is legal and must be resolved before it is
        # judged -- "/to-metadata" tells you nothing until you urljoin it.
        "/relative-to-metadata": ("redirect", "/to-metadata"),
        # An ordinary public-web chain: apex -> www -> canonical.
        "/hop-1": ("redirect", "/hop-2"),
        "/hop-2": ("redirect", "/final"),
        "/final": ("ok", "ARRIVED"),
        "/loop": ("redirect", "/loop"),
    }

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        # /status/<code>/to-metadata lets a test pick the redirect code, since
        # an attacker picks it too and urllib routes 301/302/303/307/308
        # through different handlers.
        if self.path.startswith("/status/"):
            self.send_response(int(self.path.split("/")[2]))
            self.send_header("Location", METADATA_URL)
            self.end_headers()
            return
        kind, target = self.ROUTES.get(self.path, ("ok", "DEFAULT"))
        if kind == "redirect":
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        body = target.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: ARG002 - silence the test output
        pass


@pytest.fixture(scope="module")
def redirect_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class TestRedirectChain:
    @pytest.mark.parametrize(
        "path",
        ["/to-metadata", "/chain-1", "/relative-to-metadata"],
    )
    def test_a_redirect_to_cloud_metadata_is_refused(self, redirect_server, path):
        """The #1269 chain, end to end, against a real 302.

        allow_private=True is the PERMISSIVE posture and the harness is on
        loopback -- so this passes only because the metadata range is refused
        whatever the posture, which is the property that matters here.
        """
        with pytest.raises(ValueError, match="link-local/metadata"):
            fetch_following_safe_redirects(
                redirect_server + path, allow_private=True, timeout=5
            )

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_every_redirect_status_code_is_re_checked(self, redirect_server, code):
        """An attacker picks the status code, so all five must be covered.

        urllib routes these through different handlers and does not define
        ``http_error_308`` on every version, so which of them even reach the
        no-redirect handler is a question worth answering by test rather than
        by reading the stdlib.
        """
        with pytest.raises(ValueError, match="link-local/metadata"):
            fetch_following_safe_redirects(
                f"{redirect_server}/status/{code}/to-metadata",
                allow_private=True,
                timeout=5,
            )

    def test_a_redirect_to_a_non_http_scheme_is_refused(self, redirect_server):
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            fetch_following_safe_redirects(
                redirect_server + "/to-file-scheme", allow_private=True, timeout=5
            )

    def test_a_redirect_to_a_private_address_is_refused_in_the_strict_posture(
        self, redirect_server
    ):
        """Strict posture, and the offending address is on hop 2.

        The initial URL here is loopback, which strict refuses on hop 1 -- so
        this asserts the refusal names 10.0.0.5, not 127.0.0.1. Getting that
        wrong would mean the test passes while hop 2 is never reached.
        """
        seen = []
        real = url_safety.assert_safe_outbound_url

        def spy(url, **kwargs):
            seen.append(url)
            # Let loopback through so the fetch actually reaches hop 2, then
            # apply the real strict guard to everything after it.
            if url.startswith(redirect_server):
                return real(url, allow_private=True)
            return real(url, **kwargs)

        with patch.object(url_safety, "assert_safe_outbound_url", spy):
            with pytest.raises(ValueError, match="non-public address 10.0.0.5"):
                fetch_following_safe_redirects(
                    redirect_server + "/to-private", timeout=5
                )
        assert seen == [redirect_server + "/to-private", "http://10.0.0.5/internal"]

    def test_every_hop_is_validated_with_the_same_posture(self, redirect_server):
        """The contract, asserted directly: no hop is fetched unvalidated."""
        seen = []
        real = url_safety.assert_safe_outbound_url

        def spy(url, **kwargs):
            seen.append((url, kwargs.get("allow_private")))
            return real(url, **kwargs)

        with patch.object(url_safety, "assert_safe_outbound_url", spy):
            fetch_following_safe_redirects(
                redirect_server + "/hop-1", allow_private=True, timeout=5
            )
        assert [u for u, _ in seen] == [
            redirect_server + "/hop-1",
            redirect_server + "/hop-2",
            redirect_server + "/final",
        ]
        # Same posture on every hop -- a chain that silently relaxed after the
        # first hop would be the same bug wearing a different hat.
        assert {p for _, p in seen} == {True}

    def test_a_legitimate_multi_hop_redirect_still_works(self, redirect_server):
        """The fix must not break WebFetch. Redirects are normal on the web."""
        final_url, status, body = fetch_following_safe_redirects(
            redirect_server + "/hop-1", allow_private=True, timeout=5
        )
        assert status == 200
        assert body == b"ARRIVED"
        assert final_url == redirect_server + "/final"

    def test_a_redirect_loop_terminates(self, redirect_server):
        with pytest.raises(ValueError, match="too many redirects"):
            fetch_following_safe_redirects(
                redirect_server + "/loop", allow_private=True, timeout=5, max_hops=3
            )


class TestGuardedWebFetchTool:
    """The tool the agent actually calls."""

    def _fetch(self, url):
        out = asyncio.run(guarded_web_fetch({"url": url}))
        return out["content"][0]["text"]

    def test_tool_refuses_a_redirect_to_metadata(self, redirect_server):
        # Strict posture (the tool's default): loopback is refused on hop 1,
        # which is itself correct -- the agent has no business fetching
        # loopback. The metadata chain is covered by TestRedirectChain above,
        # which is where the posture can be set.
        assert "web_fetch blocked" in self._fetch(redirect_server + "/to-metadata")

    def test_tool_refuses_metadata_directly(self):
        assert "web_fetch blocked" in self._fetch(METADATA_URL)

    def test_tool_refuses_a_non_http_scheme(self):
        assert "web_fetch blocked" in self._fetch("file:///etc/passwd")

    def test_tool_rejects_a_missing_url(self):
        assert "'url' is required" in self._fetch("")


def test_webfetch_is_no_longer_granted_to_the_agent():
    """#1269: the built-in tool must stay revoked.

    Granting it back re-opens the chain, and it would look entirely reasonable
    in a diff -- one string in a list -- which is exactly why this is pinned.
    """
    from agents.tools_pkg.models import WEB_TOOLS

    assert "WebFetch" not in WEB_TOOLS
    assert "WebSearch" in WEB_TOOLS
