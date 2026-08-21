"""
SSRF guard for agent web tools (#370 / epic #318) — a re-export of the canonical
===============================================================================

The agent runs under ``permission_mode="bypassPermissions"``, and the only
PreToolUse hook gates ``Bash`` — so a prompt-injected / LLM-chosen URL would
reach a fetch with no validation. ``hooks.py`` blocks that with the guard below.

This module used to carry its own 30-line copy of the check, and said so:
"duplicated here because the agent runtime (apps/backend) and the web-server
are separate import roots". That reasoning held right up until the copies
diverged — one grew an explicit cloud-metadata refusal that survives a posture
change, and this file did not. A drifted copy is worse than no guard at all: it
still LOOKS like a guard, so CodeQL's barrier (which is registered on
``assert_safe_outbound_url`` by name) clears nothing here and no alert ever
fires to tell you.

#1266 deduped it by bridging the import roots: this file appended
``apps/web-server`` to ``sys.path`` and imported the web-server fork of it.
That worked and failed closed, but it was a bridge, not a home — a stdlib-only
security primitive shared by two runtimes reached by a path shim from one of
them.

#1270 removes the bridge. The guard's home is ``factory_common.url_safety``, the
fleet canonical vendored byte-identically into BOTH ``apps/backend`` and
``apps/web-server`` from the Factory hub (Factory#154/#161), so each runtime
imports it from its own root with no path manipulation at all. Nothing was added
to ``factory_common`` here — the module was already vendored, and its docstring
already names this issue as the reason it lives there.

If this import fails, the whole ``security`` package fails to import and the
agent will not start. That is the correct direction to fail: an agent with a web
tool and no SSRF guard is the thing #370 exists to prevent.

One behavioural note, and #1361 closed the half of it that was a leak. The
canonical used to raise plain ``ValueError`` while the web-server's fork raised
``InputRejectedError``, and its resolve-failure branch interpolated the
``socket.gaierror`` into the message. ``hooks.py`` and ``tools/web.py`` both put
that message into text the agent reads back, so the resolver's wording crossed
a boundary nobody here wrote it for. Factory#831 made the canonical raise
``InputRejectedError`` (still a ``ValueError`` subclass, so both callers here
keep catching) and dropped the ``gaierror`` from the text, keeping it on
``__cause__``. With that, the web-server's fork had no reason left to exist and
#1361 deleted it: there is one guard in the fleet now, this one.

Note on DNS rebinding: the guard resolves the host and checks the resolved IP,
then the transport re-resolves at fetch time (TOCTOU). Unchanged by this
refactor; closing it needs IP-pinning at the transport layer. Residual on #370.

Note on redirects — CLOSED in #1269, and worth reading because the fix is not
in this file. This hook could never close it: it validates a URL and then
something else fetches, and that something else followed 30x. The answer was to
stop being the second party — ``WebFetch`` is no longer granted to the agent,
and ``mcp__aifactory__web_fetch`` does the fetching in-process through
``fetch_following_safe_redirects``, which re-checks every hop.

The hook still runs, and still matters: it is what refuses the FIRST url, it
covers ``WebSearch``, and it is what fails closed if ``WebFetch`` is ever
granted again by an edit to ``core/client.py``. Defence in depth, not
redundancy.
"""

from __future__ import annotations

from factory_common.url_safety import (
    assert_safe_outbound_url,
    fetch_following_safe_redirects,
)

# The historical name, kept so `hooks.py` and #370's tests read unchanged. Same
# strict posture (public addresses only), and still caught by `except
# ValueError` -- `InputRejectedError` subclasses it.
assert_url_not_ssrf = assert_safe_outbound_url

__all__ = [
    "assert_safe_outbound_url",
    "assert_url_not_ssrf",
    "fetch_following_safe_redirects",
]
