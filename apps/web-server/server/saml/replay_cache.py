"""SAML assertion replay-attack defence (Epic #35 #41).

Why this exists
---------------

A signed SAML assertion is valid for a window the IdP chose
(``NotBefore`` → ``NotOnOrAfter``). Without a replay cache, an
attacker who captures one valid assertion can submit it again and
again within that window. Defence: keep an in-memory record of every
``assertion_id`` we've already accepted and reject duplicates until
the assertion's own validity expires.

Why per-assertion TTL (not a blanket 5-min LRU)
-----------------------------------------------

A blanket short TTL is the common SCIM/SAML mistake. Real-world
windows: Azure AD = 60-120 min, Okta = 3 hours. A 5-minute LRU lets
an attacker replay after 5 minutes for the remainder of the
assertion's life. We instead track each assertion until its own
``NotOnOrAfter``.

Bound on memory
---------------

Capped at ``max_size`` entries (default 10,000) per replica. Overflow
evicts the LRU entry AND logs a WARNING — overflow under normal load
means either a misconfigured IdP issuing very-long-lived assertions
or an active attack. Either way operators need to know.

This is a per-replica cache; with ``replicas > 1`` an assertion
captured against pod A and replayed against pod B would slip through.
Acceptable for v1.1 because (a) the load balancer's session affinity
typically pins repeat hits, (b) the attacker still needs to capture
the assertion in the first place (TLS), and (c) cross-replica replay
defence requires a shared store (Redis SETNX) that we can add later
without changing the public surface.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class SamlReplayCache:
    """Thread-safe ordered-dict-backed replay cache.

    Each entry maps ``assertion_id`` (str) to ``exp_epoch`` (float).
    A pre-insertion sweep drops expired entries so memory doesn't
    grow with traffic during quiet IdP-rotation periods.
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._max_size = max_size
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, assertion_id: str, exp_epoch: float) -> bool:
        """Return True if assertion is fresh; False if seen already.

        Atomically:
          1. Sweep expired entries.
          2. Reject if ``assertion_id`` already in cache.
          3. Otherwise record + return True.

        ``exp_epoch`` is the assertion's ``NotOnOrAfter`` in unix
        time. A past/equal-to-now value is treated as "already
        expired" (rejected — caller's outer validation should also
        catch this, but defence in depth).
        """
        now = time.time()
        if exp_epoch <= now:
            logger.debug(
                "replay cache: rejecting expired assertion id=%s exp=%s now=%s",
                assertion_id,
                exp_epoch,
                now,
            )
            return False

        with self._lock:
            self._sweep_locked(now)

            if assertion_id in self._entries:
                logger.warning(
                    "replay cache: REJECTED replay of assertion id=%s "
                    "(seen previously, original expiry=%s)",
                    assertion_id,
                    self._entries[assertion_id],
                )
                return False

            if len(self._entries) >= self._max_size:
                # LRU eviction. Overflow under normal load is unusual —
                # log so operators can tune max_size if their IdP issues
                # very-long-lived assertions.
                evicted_id, _ = self._entries.popitem(last=False)
                logger.warning(
                    "replay cache: at capacity (%d); evicted LRU id=%s "
                    "before inserting id=%s. Consider raising max_size if "
                    "this fires under steady-state load.",
                    self._max_size,
                    evicted_id,
                    assertion_id,
                )

            self._entries[assertion_id] = exp_epoch
            return True

    def _sweep_locked(self, now: float) -> None:
        """Drop entries whose ``exp_epoch`` is in the past. Caller
        holds the lock."""
        # OrderedDict doesn't sort by value; we walk linearly. At
        # typical sizes (< 10k) this is microseconds and only fires
        # on insertion, not per request.
        expired = [k for k, exp in self._entries.items() if exp <= now]
        for k in expired:
            del self._entries[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Drop all entries — for tests and operational reset."""
        with self._lock:
            self._entries.clear()
