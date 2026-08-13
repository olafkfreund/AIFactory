"""Tests for the SAML assertion replay-cache (Epic #35 #41).

Covers:
- Fresh assertion is accepted; same id re-submitted is rejected.
- Per-assertion TTL (not blanket): an assertion with a later expiry
  outlives one with an earlier expiry.
- Already-expired assertion is rejected without consuming a slot.
- Overflow at ``max_size`` evicts LRU + logs WARNING.
- Pre-insertion sweep drops expired entries to free space.
- Thread-safety: concurrent check_and_add for the same id only
  permits one.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.saml.replay_cache import SamlReplayCache


def _future(seconds: float = 60.0) -> float:
    return time.time() + seconds


def test_fresh_assertion_accepted():
    cache = SamlReplayCache()
    assert cache.check_and_add("a-001", _future()) is True


def test_replay_rejected(caplog):
    cache = SamlReplayCache()
    cache.check_and_add("a-001", _future())
    with caplog.at_level("WARNING"):
        accepted = cache.check_and_add("a-001", _future())
    assert accepted is False
    assert any("REJECTED replay" in r.message for r in caplog.records)


def test_expired_assertion_rejected():
    """An assertion whose NotOnOrAfter is in the past is rejected and
    does NOT consume a cache slot."""
    cache = SamlReplayCache()
    past = time.time() - 60
    assert cache.check_and_add("a-001", past) is False
    assert len(cache) == 0  # didn't consume a slot


def test_per_assertion_ttl_outlives_neighbors():
    """An assertion with a long expiry must still be in-cache after a
    short-expiry neighbour has been swept. This is the test that would
    catch a blanket-LRU regression."""
    cache = SamlReplayCache()
    # Short one expires in 0.5s
    cache.check_and_add("short", time.time() + 0.5)
    # Long one expires in 60s
    cache.check_and_add("long", time.time() + 60)
    assert len(cache) == 2

    # Wait for short to expire, then trigger a sweep via insertion.
    time.sleep(0.7)
    cache.check_and_add("trigger", time.time() + 60)
    assert len(cache) == 2  # short was swept; trigger added; long still in
    # Replay of long is still rejected (the important property)
    assert cache.check_and_add("long", time.time() + 60) is False


def test_overflow_evicts_lru_and_warns(caplog):
    cache = SamlReplayCache(max_size=3)
    cache.check_and_add("a", _future())
    cache.check_and_add("b", _future())
    cache.check_and_add("c", _future())
    assert len(cache) == 3

    with caplog.at_level("WARNING"):
        cache.check_and_add("d", _future())

    assert len(cache) == 3
    # LRU 'a' should be gone; b/c/d remain.
    assert cache.check_and_add("a", _future()) is True  # re-acceptable
    # That last insertion bumped us to 4, then triggered another eviction.
    # The important property: the WARN log fired on the overflow.
    assert any("at capacity" in r.message for r in caplog.records)


def test_concurrent_replay_only_one_succeeds():
    """If two threads submit the same assertion concurrently, only
    ONE check_and_add returns True. Without the lock this races."""
    cache = SamlReplayCache()
    exp = _future()
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # release all together
        results.append(cache.check_and_add("contended", exp))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"exactly one thread should win; got {results}"
    assert results.count(False) == 7


def test_clear_drops_everything():
    cache = SamlReplayCache()
    cache.check_and_add("a", _future())
    cache.check_and_add("b", _future())
    cache.clear()
    assert len(cache) == 0
    # Previously-rejected ids can now be re-added.
    assert cache.check_and_add("a", _future()) is True


def test_sweep_does_not_drop_unexpired():
    """The pre-insertion sweep walks all entries; ensure it leaves
    unexpired ones in place."""
    cache = SamlReplayCache()
    cache.check_and_add("short", time.time() + 0.3)
    cache.check_and_add("long", time.time() + 60)
    time.sleep(0.5)
    # New insertion triggers sweep.
    cache.check_and_add("new", time.time() + 60)
    # short is gone; long + new remain.
    assert len(cache) == 2
    # long is still seen as replay.
    assert cache.check_and_add("long", time.time() + 60) is False
