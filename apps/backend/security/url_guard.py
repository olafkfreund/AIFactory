"""
SSRF guard for agent web tools (#370 / epic #318) — now a re-export
==================================================================

The agent runs under ``permission_mode="bypassPermissions"`` with ``WebFetch``
granted, and the only PreToolUse hook gates ``Bash`` — so a prompt-injected /
LLM-chosen URL reaches the fetch with no validation. ``hooks.py`` blocks that
with the guard below.

This module used to carry its own 30-line copy of the check, and said so:
"duplicated here because the agent runtime (apps/backend) and the web-server
are separate import roots". That reasoning held right up until the copies
diverged — ``services/url_safety.py`` grew an explicit cloud-metadata refusal
that survives a posture change, and this file did not. A drifted copy is worse
than no guard at all: it still LOOKS like a guard, so CodeQL's barrier (which
is registered on ``assert_safe_outbound_url`` by name) clears nothing here and
no alert ever fires to tell you.

So the import root is bridged instead. The whole repo ships in one image
(``COPY . /home/projects/MagesticAI/``), and the web-server already reaches the
other way for the same reason — see ``server/routes/mcp.py`` and
``tasks_usage.py``, which add ``apps/backend`` to ``sys.path`` to import the
catalog and attribution modules. ``server.services.url_safety`` is stdlib-only
and ``server/__init__.py`` / ``server/services/__init__.py`` are empty, so
nothing web-serverish is dragged into the agent runtime by this.

If this import fails, the whole ``security`` package fails to import and the
agent will not start. That is the correct direction to fail: an agent running
with ``WebFetch`` and no SSRF guard is the thing #370 exists to prevent.

The permanent home is ``factory_common`` — the repo's stdlib-only
"importable anywhere" layer, which exists for exactly this. It is vendored
byte-exact from the Factory hub behind a drift gate, so moving the guard there
has to land in the hub first and cannot be done from this repo alone. Tracked
in #1270.

Note on DNS rebinding: the guard resolves the host and checks the resolved IP,
then the SDK re-resolves at fetch time (TOCTOU). Unchanged by this refactor;
closing it needs IP-pinning at the transport layer, which the SDK's WebFetch
tool does not expose. Residual on #370.

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

import sys
from pathlib import Path

_WEB_SERVER = Path(__file__).resolve().parents[2] / "web-server"
if str(_WEB_SERVER) not in sys.path:
    # Appended, not inserted at 0: this only needs to make `server` resolvable,
    # and `server` is a generic enough name that jumping the queue ahead of
    # site-packages would be a way to shadow somebody else's module.
    sys.path.append(str(_WEB_SERVER))

from server.services.url_safety import (  # noqa: E402
    assert_safe_outbound_url,
    fetch_following_safe_redirects,
)

# The historical name, kept so `hooks.py` and #370's tests read unchanged. Same
# strict posture (public addresses only) and same `ValueError` as the copy it
# replaces.
assert_url_not_ssrf = assert_safe_outbound_url

__all__ = [
    "assert_safe_outbound_url",
    "assert_url_not_ssrf",
    "fetch_following_safe_redirects",
]
