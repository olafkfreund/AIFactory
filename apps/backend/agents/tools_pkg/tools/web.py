"""
Guarded Web Fetch (#1269)
=========================

Replaces the built-in ``WebFetch`` for the agent, because the built-in one
could not be made safe from where we stood.

The shape of the bug: ``web_fetch_security_hook`` is a PreToolUse hook. It sees
the URL the model asked for, validates it, returns "allow" -- and then something
else does the fetching. So::

    WebFetch https://attacker.example/      <- validated, resolves public, allowed
      -> 302 Location: http://169.254.169.254/latest/meta-data/iam/...
      -> whatever the fetcher decides to do about that

The dangerous URL is the one we never see. Note what IS and IS NOT established
here, because the difference matters for how much this module is allowed to
claim:

VERIFIED -- the hook cannot act on any URL after the first. Its whole vocabulary
is ``HookSpecificOutput`` (claude_agent_sdk types.py): ``permissionDecision``,
``permissionDecisionReason``, ``updatedInput``. Allow, deny, or rewrite the
input. There is no "and here is the response, don't fetch it yourself", and
nothing that constrains redirect handling.

NOT INDEPENDENTLY VERIFIED -- that the built-in fetcher does in fact follow a
30x to a private address. It is a compiled binary; that claim came from the
#1269 report and was not confirmed here.

The second point does not weaken the case, it just relocates it. Whether the
built-in tool happens to block metadata today is not a property we control,
test, or pin (``claude-agent-sdk>=0.1.16`` is a floor, not a pin), and a
guarantee that rests on undocumented behaviour of a dependency that can float
under us is not a guarantee. Our guard promised something it had no mechanism
to deliver. That is true regardless of what the binary does.

A pre-flight that walks the chain itself does not fix it either -- it validates
hops WE see, then the real fetcher asks again, and a server that redirects
differently on the second request is exactly the attacker in this threat model.

So the fetch comes back in-process. ``WebFetch`` is no longer granted; this
tool is, and it follows redirects through
:func:`fetch_following_safe_redirects`, which re-validates every hop against
the same guard before requesting it. There is no other fetcher left to be
tricked.

What is deliberately NOT done: refusing redirects outright. http -> https,
apex -> www and shortened links are ordinary on the public web, and a research
tool that dies on them is a tool the agent routes around.

What this costs, stated plainly: the built-in WebFetch converted HTML to
markdown and ran a prompt over it. This returns the response body, truncated.
The model reads HTML perfectly well, and a smaller, honest tool beat a nicer
one we could not secure.
"""

from __future__ import annotations

from typing import Any

# security.url_guard is the one place that bridges apps/backend to the
# canonical guard (#1265). Importing through it rather than adding a second
# sys.path shim is the whole point of that module.
#
# No `type: ignore[import-not-found]` here, despite a bare
# `mypy --config-file standards/mypy.ini <this file>` reporting one: the gate is
# scripts/cq_ratchet.py, which invokes mypy so that `security.*` resolves, and
# there the ignore is an `unused-ignore` error instead. Two invocations, two
# opposite verdicts -- this file follows the one CI actually runs.
from security.url_guard import fetch_following_safe_redirects

tool: Any
try:
    from claude_agent_sdk import tool

    SDK_TOOLS_AVAILABLE = True
except ImportError:
    # The SDK is optional here: this module is imported by the tool registry,
    # which returns an empty tool list when the SDK is absent.
    tool = None
    SDK_TOOLS_AVAILABLE = False

# Enough for documentation and API pages; a build agent that needs more than
# this from one URL is doing something the tool is not for.
MAX_BODY_CHARS = 100_000

# Cloudflare 403s the default Python-urllib UA, which would make this tool look
# broken on a large slice of the documentation web. Same reasoning, and the same
# string, as factory_common.http.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


TOOL_DESCRIPTION = (
    "Fetch a public http(s) URL and return its body as text. Use this for "
    "documentation and research. Redirects are followed, but every hop is "
    "re-checked, and private, loopback and cloud-metadata addresses are "
    "refused at every hop."
)


async def guarded_web_fetch(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch a URL with the SSRF guard applied to every redirect hop.

    Deliberately a module-level function rather than a closure inside
    ``create_web_tools``: the security behaviour is then testable without going
    anywhere near the SDK's ``@tool`` decorator, which some test runs replace
    with a mock. A guard whose test only runs when an optional dependency is
    importable is a guard that will one day stop being tested and nobody will
    notice.
    """
    url = args.get("url")
    if not isinstance(url, str) or not url:
        return _text("web_fetch: 'url' is required and must be a string.")

    try:
        final_url, status, body = fetch_following_safe_redirects(url, headers=_HEADERS)
    except ValueError as exc:
        # The guard refused it -- initial URL or any hop.
        return _text(
            f"web_fetch blocked: {exc}. Only public http(s) URLs are "
            "permitted; private/loopback/metadata hosts and non-http "
            "schemes are refused, including via redirects."
        )
    except Exception as exc:  # noqa: BLE001 - report, never leak a traceback
        return _text(f"web_fetch failed: {type(exc).__name__}: {exc}")

    text = body.decode("utf-8", errors="replace")
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n...[truncated]"
    note = f" (after redirect to {final_url})" if final_url != url else ""
    return _text(f"HTTP {status} from {url}{note}\n\n{text}")


def create_web_tools() -> list[Any]:
    """Create the guarded web-fetch tool (#1269)."""
    if not SDK_TOOLS_AVAILABLE:
        return []

    return [tool("web_fetch", TOOL_DESCRIPTION, {"url": str})(guarded_web_fetch)]


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}
