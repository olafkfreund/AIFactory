"""
Claude token pool tests (RFC-0016 #670)
=======================================

Admission control (#672) runs MULTIPLE builds concurrently in one pod. The pool
must hand DISTINCT credentials to concurrent builds when several are configured,
and SHARE the single token when only one is (no behaviour change for today's
one-token deployment).
"""

import json
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from server.services.claude_token_pool import (  # noqa: E402
    ClaudeTokenPool,
    Credential,
)


def _write_profiles(path: Path, tokens: list[str]) -> None:
    profiles = [
        {"id": f"p{i}", "name": f"Profile {i}", "oauthToken": tok}
        for i, tok in enumerate(tokens)
    ]
    path.write_text(json.dumps({"profiles": profiles, "activeProfileId": "p0"}))


class TestPoolDistribution:
    def test_n_tokens_n_concurrent_builds_get_distinct(self, tmp_path: Path) -> None:
        """With N tokens configured, N concurrent builds get N DISTINCT tokens."""
        n = 4
        _write_profiles(
            tmp_path / "claude-profiles.json", [f"tok-{i}" for i in range(n)]
        )
        pool = ClaudeTokenPool.from_sources(tmp_path)
        assert pool.size == n

        results: dict[str, Credential] = {}
        start = threading.Barrier(n)
        lock = threading.Lock()

        def build(task_id: str) -> None:
            start.wait()  # maximize the race on checkout
            cred = pool.checkout(task_id)
            assert cred is not None
            with lock:
                results[task_id] = cred

        threads = [
            threading.Thread(target=build, args=(f"task-{i}",)) for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        tokens = [c.token for c in results.values()]
        assert len(results) == n
        # The whole point: every concurrent build got a DISTINCT token.
        assert len(set(tokens)) == n, f"builds collided on tokens: {tokens}"

    def test_single_token_is_shared(self, tmp_path: Path) -> None:
        """With ONE token, concurrent builds SHARE it (today's behaviour)."""
        _write_profiles(tmp_path / "claude-profiles.json", ["only-tok"])
        pool = ClaudeTokenPool.from_sources(tmp_path)
        assert pool.size == 1

        c1 = pool.checkout("task-1")
        c2 = pool.checkout("task-2")
        assert c1 is not None and c2 is not None
        assert c1.token == c2.token == "only-tok"

    def test_more_builds_than_tokens_falls_back_to_lru_sharing(
        self, tmp_path: Path
    ) -> None:
        """More builds than tokens: extras share, but distinct ones go out first."""
        _write_profiles(tmp_path / "claude-profiles.json", ["a", "b"])
        pool = ClaudeTokenPool.from_sources(tmp_path)

        c1 = pool.checkout("t1")
        c2 = pool.checkout("t2")
        c3 = pool.checkout("t3")  # third build, only 2 tokens
        assert c1 is not None and c2 is not None and c3 is not None
        # First two are distinct (both tokens used once before any reuse).
        assert c1.token != c2.token
        # Third reuses one of them (the LRU), never invents a token.
        assert c3.token in {"a", "b"}

    def test_release_returns_credential_to_pool(self, tmp_path: Path) -> None:
        """A released credential is preferred again over a busy one."""
        _write_profiles(tmp_path / "claude-profiles.json", ["a", "b"])
        pool = ClaudeTokenPool.from_sources(tmp_path)

        c1 = pool.checkout("t1")
        c2 = pool.checkout("t2")
        assert c1 is not None and c2 is not None
        assert {c1.token, c2.token} == {"a", "b"}

        # Free t1's credential; a NEW build should get it back (it's now free,
        # while c2's is still in use).
        freed = c1.token
        pool.release("t1")
        c3 = pool.checkout("t3")
        assert c3 is not None
        assert c3.token == freed, "released free credential was not reused first"

    def test_checkout_is_idempotent_per_task(self, tmp_path: Path) -> None:
        """Re-checkout for the same task returns the same credential."""
        _write_profiles(tmp_path / "claude-profiles.json", ["a", "b"])
        pool = ClaudeTokenPool.from_sources(tmp_path)
        first = pool.checkout("t1")
        again = pool.checkout("t1")
        assert first is not None and again is not None
        assert first.token == again.token

    def test_release_is_idempotent_and_safe_for_unknown_task(
        self, tmp_path: Path
    ) -> None:
        _write_profiles(tmp_path / "claude-profiles.json", ["a"])
        pool = ClaudeTokenPool.from_sources(tmp_path)
        pool.release("never-checked-out")  # must not raise
        pool.checkout("t1")
        pool.release("t1")
        pool.release("t1")  # double release must not raise / underflow


class TestPoolSources:
    def test_env_pool_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLAUDE_CODE_OAUTH_TOKEN_POOL is parsed into distinct entries."""
        _write_profiles(tmp_path / "claude-profiles.json", ["ignored"])
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_POOL", "e1, e2 ; e3")
        pool = ClaudeTokenPool.from_sources(tmp_path)
        assert pool.size == 3
        tokens = set()
        for i in range(3):
            cred = pool.checkout(f"t{i}")
            assert cred is not None
            tokens.add(cred.token)
        assert tokens == {"e1", "e2", "e3"}

    def test_single_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no profiles file, a single env token forms a size-1 pool."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_POOL", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
        pool = ClaudeTokenPool.from_sources(tmp_path)  # empty dir, no profiles
        assert pool.size == 1
        cred = pool.checkout("t1")
        assert cred is not None
        assert cred.token == "env-tok"
        assert cred.profile_id == "env-override"

    def test_empty_pool_when_no_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_POOL", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-home")
        pool = ClaudeTokenPool.from_sources(tmp_path)
        assert pool.is_empty()
        assert pool.checkout("t1") is None

    def test_duplicate_tokens_collapse(self, tmp_path: Path) -> None:
        """Two profiles with the same token are ONE distinct credential."""
        _write_profiles(tmp_path / "claude-profiles.json", ["dup", "dup", "uniq"])
        pool = ClaudeTokenPool.from_sources(tmp_path)
        assert pool.size == 2


class TestAgentServiceWiring:
    """The AgentService entrypoints draw from / return to the pool (#670)."""

    def _service(self, tmp_path: Path):
        from server.services.agent_service import AgentService

        service = AgentService()
        service.settings.PROJECTS_DATA_DIR = str(tmp_path)
        return service

    def test_concurrent_builds_get_distinct_tokens(self, tmp_path: Path) -> None:
        """N concurrent _resolve_claude_token_pooled calls get DISTINCT tokens."""
        n = 4
        _write_profiles(
            tmp_path / "claude-profiles.json", [f"tok-{i}" for i in range(n)]
        )
        service = self._service(tmp_path)

        results: dict[str, str] = {}
        start = threading.Barrier(n)
        lock = threading.Lock()

        def build(task_id: str) -> None:
            start.wait()
            token, _pid, _name = service._resolve_claude_token_pooled(task_id)
            with lock:
                results[task_id] = token

        threads = [
            threading.Thread(target=build, args=(f"task-{i}",)) for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        tokens = list(results.values())
        assert len(tokens) == n
        assert len(set(tokens)) == n, f"concurrent builds collided: {tokens}"

    def test_single_token_shared_no_behaviour_change(self, tmp_path: Path) -> None:
        """One configured token -> concurrent builds share it (today's behaviour)."""
        _write_profiles(tmp_path / "claude-profiles.json", ["only"])
        service = self._service(tmp_path)
        t1, _, _ = service._resolve_claude_token_pooled("task-1")
        t2, _, _ = service._resolve_claude_token_pooled("task-2")
        assert t1 == t2 == "only"

    def test_release_frees_token_for_next_build(self, tmp_path: Path) -> None:
        """Releasing a build's credential lets a later build reuse it."""
        _write_profiles(tmp_path / "claude-profiles.json", ["a", "b"])
        service = self._service(tmp_path)

        t1, _, _ = service._resolve_claude_token_pooled("task-1")
        t2, _, _ = service._resolve_claude_token_pooled("task-2")
        assert {t1, t2} == {"a", "b"}

        # Build 1 ends -> its token returns to the pool; a new build reuses it.
        service._release_task_credential("task-1")
        t3, _, _ = service._resolve_claude_token_pooled("task-3")
        assert t3 == t1, "released token was not reused by the next build"
