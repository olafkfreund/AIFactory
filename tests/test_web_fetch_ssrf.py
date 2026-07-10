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

import pytest
from security.hooks import web_fetch_security_hook
from security.url_guard import assert_url_not_ssrf

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
        # Returns None (does not raise) for a safe URL.
        assert assert_url_not_ssrf(url) is None


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
