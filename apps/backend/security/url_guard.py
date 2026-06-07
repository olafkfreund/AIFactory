"""
SSRF guard for agent web tools (#370 / epic #318)
=================================================

The agent runs under ``permission_mode="bypassPermissions"`` with ``WebFetch``
granted, and the only PreToolUse hook gates ``Bash`` — so a prompt-injected /
LLM-chosen URL reaches the fetch with no validation. This guard rejects URLs
whose host resolves to a non-public address (cloud metadata 169.254.169.254,
loopback, RFC-1918 private ranges, link-local, reserved) and non-http(s)
schemes (``file://`` etc.).

Stdlib-only. Mirrors ``_assert_url_not_ssrf`` added for the LLM ``base_url`` in
#323 (``apps/web-server/server/routes/llm_endpoints.py``); duplicated here
because the agent runtime (apps/backend) and the web-server are separate import
roots.

Note on DNS rebinding: this resolves the host and checks the resolved IP, then
the SDK re-resolves at fetch time (TOCTOU). That window is the same posture as
#323; fully closing it needs IP-pinning at the transport layer, which the SDK
WebFetch tool does not expose. Tracked as residual in #370.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse


def assert_url_not_ssrf(url: str) -> None:
    """Raise ``ValueError`` if ``url`` is non-http(s) or resolves to a
    non-public address (SSRF). Returns ``None`` when the URL is safe to fetch."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {parsed.scheme!r} (only http/https)")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")

    default_port = 443 if parsed.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(
            host, parsed.port or default_port, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve host {host!r}: {exc}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local  # 169.254.0.0/16 — cloud metadata
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(f"refusing to fetch non-public address {ip} (SSRF)")
