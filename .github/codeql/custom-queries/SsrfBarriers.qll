/**
 * The SSRF barrier for this repo.
 *
 * `assert_safe_outbound_url` (apps/web-server/server/services/url_safety.py) is
 * the one place outbound URLs are checked: http(s)-only scheme, a hard refusal
 * of the cloud metadata range in both postures, and — unless the call site
 * passes `allow_private=True` for a deliberately local target such as a
 * self-hosted Ollama — a requirement that the host resolve to a public address.
 * It raises rather than sanitising, and it RETURNS the url so a call site is
 * forced to use the value that came out of the check; the barrier is registered
 * on the call, so only that returned value is cleared. Checking one string and
 * fetching another still reports, which is the point.
 *
 * Deliberately NOT registered: `urllib.parse.urlparse`, `str.startswith("http")`
 * and friends. Parsing a URL tells you nothing about where it resolves, and a
 * `startswith` check passes `http://169.254.169.254/` unharmed. Registering
 * either would clear the exact alerts this pack exists to keep honest.
 *
 * `allow_private=True` sites are cleared by this barrier too. That posture is a
 * narrower guarantee, not an absent one — scheme and metadata are still
 * enforced — and reaching an operator-configured private address is the
 * documented purpose of those endpoints, so there is no alert left to raise.
 * The residual risk (DNS rebinding, and a hostile service on the LAN of a
 * machine the operator already controls) is recorded in url_safety.py.
 */

import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.security.dataflow.ServerSideRequestForgeryCustomizations

/** A call to `assert_safe_outbound_url`, and the guard's own parameter. */
class UrlSafetyBarrier extends ServerSideRequestForgery::Sanitizer {
  UrlSafetyBarrier() {
    exists(DataFlow::CallCfgNode call |
      call.getFunction().asExpr().(Name).getId() = "assert_safe_outbound_url"
      or
      call.getFunction().asExpr().(Attribute).getName() = "assert_safe_outbound_url"
    |
      this = call
    )
    or
    // The guard's own first parameter. Without this, the `urlparse(url)` and
    // `getaddrinfo(host, ...)` INSIDE the guard re-report the very flow the
    // barrier exists to clear — the helper would indict itself.
    exists(Function f |
      f.getName() = "assert_safe_outbound_url" and
      this.(DataFlow::ParameterNode).getParameter() = f.getArg(0)
    )
  }
}
