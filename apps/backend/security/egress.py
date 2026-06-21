"""Agent egress control (#363 AC3) — a command-layer defense-in-depth guard.

The dynamic command allowlist permits common network tools (`curl`, `wget`,
`ssh`, …) because builds legitimately need them. That leaves a hole: a
prompt-injected *but allowlisted* command can still reach the network and
exfiltrate whatever the agent can read. The real boundary is a pod-level
NetworkPolicy (default-deny egress); this module is the in-process compensating
control so the protection also holds in runtimes without that policy.

Opt-in via ``AIFACTORY_EGRESS_POLICY`` (default ``off`` → no behavior change):

* ``off``        — disabled; existing behavior.
* ``deny``       — block any network-egress command outright.
* ``allowlist``  — block an egress command unless **every** host it references
                   is in ``AIFACTORY_EGRESS_ALLOWED_HOSTS`` (comma-separated).
                   If no host can be confidently extracted, fail closed (block).

Kept deliberately conservative: in ``allowlist`` mode an egress command whose
targets we can't fully parse is blocked, not allowed.
"""

from __future__ import annotations

import os
import re

# Commands that initiate outbound network connections. Names only — matched
# against the parsed command list, so `mycurl` or `curler` won't trip it.
EGRESS_COMMANDS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "ftps",
        "tftp",
        "rsync",
        "socat",
        "lynx",
        "links",
        "aria2c",
        "httpie",
    }
)

_TRUTHY = {"deny", "allowlist"}

# Host extractors (best-effort, conservative): full URLs, scp/ssh user@host,
# and bare host:port. We only need the hostname to compare against the allowlist.
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://([^/\s:?#]+)", re.I)
_USERHOST_RE = re.compile(r"\b[\w.\-]+@([A-Za-z0-9.\-]+)")
_HOSTPORT_RE = re.compile(r"\b([a-zA-Z0-9][a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}):\d+")


def egress_policy() -> str:
    """Return the active policy: ``off`` | ``deny`` | ``allowlist``."""
    val = os.environ.get("AIFACTORY_EGRESS_POLICY", "off").strip().lower()
    return val if val in _TRUTHY else "off"


def allowed_hosts() -> set[str]:
    """Parse ``AIFACTORY_EGRESS_ALLOWED_HOSTS`` into a lowercased host set."""
    raw = os.environ.get("AIFACTORY_EGRESS_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def extract_hosts(command: str) -> set[str]:
    """Best-effort hostnames an egress command would contact. Lowercased,
    port/userinfo stripped. Conservative: only confident matches."""
    hosts: set[str] = set()
    for rx in (_URL_RE, _USERHOST_RE, _HOSTPORT_RE):
        for m in rx.findall(command or ""):
            host = m.split("@")[-1].split(":")[0].strip().lower()
            if host:
                hosts.add(host)
    return hosts


def _host_allowed(host: str, allow: set[str]) -> bool:
    """Allow exact matches and subdomains of an allowlisted apex (e.g.
    ``api.github.com`` matches an allowlisted ``github.com``)."""
    host = host.lower()
    return any(host == a or host.endswith("." + a) for a in allow)


def check_egress(command: str, commands: list[str]) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the egress policy. ``allowed=True`` with
    an empty reason when the policy is off or no egress command is present."""
    policy = egress_policy()
    if policy == "off":
        return True, ""
    present = sorted({c for c in commands if c in EGRESS_COMMANDS})
    if not present:
        return True, ""

    if policy == "deny":
        return False, (
            f"Egress command(s) {present} blocked by AIFACTORY_EGRESS_POLICY=deny. "
            "Network access from agent commands is disabled."
        )

    # allowlist mode
    allow = allowed_hosts()
    hosts = extract_hosts(command)
    if not hosts:
        return False, (
            f"Egress command(s) {present} blocked: no target host could be parsed "
            "for allowlist verification (AIFACTORY_EGRESS_POLICY=allowlist, fail-closed)."
        )
    disallowed = sorted(h for h in hosts if not _host_allowed(h, allow))
    if disallowed:
        return False, (
            f"Egress to {disallowed} blocked: not in AIFACTORY_EGRESS_ALLOWED_HOSTS "
            f"({sorted(allow) or 'empty'})."
        )
    return True, ""
