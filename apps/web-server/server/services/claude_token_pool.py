"""
Claude token pool for concurrent builds (RFC-0016 #670)
=======================================================

Admission control (#672) lets MULTIPLE independent builds run concurrently in
one pod. Before this module every build resolved the SAME OAuth token from the
single active profile (``agent_service._resolve_claude_token``), so N concurrent
builds collided on one token's rate limit.

This pool hands DISTINCT credentials to concurrent builds when more than one is
configured, and falls back to the single shared token when only one is — i.e.
ZERO behaviour change for the one-token deployment that exists today.

Credential sources (a credential = ``(token, profile_id, profile_name)``):
  1. ``CLAUDE_CODE_OAUTH_TOKEN_POOL`` env var — comma/newline/semicolon
     separated list of tokens (each becomes a distinct pool entry). Lets an
     operator widen the pool without editing the profiles file.
  2. ``claude-profiles.json`` — every profile that carries a token (the
     multi-profile config that already exists for failover).
  3. ``CLAUDE_CODE_OAUTH_TOKEN`` env var — the single env-override token.
  4. ``~/.claude/oauth_token`` — the static fallback file.
The first source that yields any credentials wins, mirroring the precedence of
the legacy single-token resolver.

Checkout policy: least-recently-used across DISTINCT credentials. A concurrent
build draws a credential no other live build holds; when every distinct
credential is already checked out (more builds than tokens) it shares the LRU
one (today's collide-on-one behaviour, but spread across the pool). When only
one credential exists, all builds share it — identical to before.

Concurrency: a ``threading.Lock`` guards checkout/return. The pool is consumed
from async code but the critical sections are tiny and synchronous, so a plain
lock is simpler and deadlock-free versus an asyncio lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_POOL_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN_POOL"
_SINGLE_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_POOL_SPLIT_RE = re.compile(r"[,;\n]+")


@dataclass(frozen=True)
class Credential:
    """One Claude credential the pool can hand to a build."""

    token: str
    profile_id: str
    profile_name: str


@dataclass
class _Entry:
    cred: Credential
    in_use: int = 0  # how many live builds currently hold this credential
    last_handed: int = 0  # monotonically increasing checkout sequence (LRU)


@dataclass
class ClaudeTokenPool:
    """Concurrency-safe pool of distinct Claude credentials.

    Build the pool with :meth:`from_sources`; check a credential out for a build
    with :meth:`checkout` and return it with :meth:`release`.
    """

    _entries: list[_Entry] = field(default_factory=list)
    _by_profile: dict[str, _Entry] = field(default_factory=dict)
    _checked_out: dict[str, str] = field(default_factory=dict)  # task_id -> profile_id
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _seq: int = 0

    # ---- construction -----------------------------------------------------

    @classmethod
    def from_sources(
        cls,
        projects_data_dir: str | os.PathLike[str] | None,
        legacy_profiles_file: Path | None = None,
    ) -> ClaudeTokenPool:
        """Discover credentials and build a pool (see module docstring)."""
        creds = _discover_credentials(projects_data_dir, legacy_profiles_file)
        pool = cls()
        for cred in creds:
            entry = _Entry(cred=cred)
            pool._entries.append(entry)
            pool._by_profile[cred.profile_id] = entry
        return pool

    # ---- introspection ----------------------------------------------------

    @property
    def size(self) -> int:
        """Number of DISTINCT credentials in the pool."""
        return len(self._entries)

    def is_empty(self) -> bool:
        return not self._entries

    # ---- checkout / release ----------------------------------------------

    def checkout(
        self, task_id: str, exclude_profile_id: str | None = None
    ) -> Credential | None:
        """Reserve a credential for ``task_id``.

        Prefers a credential that NO live build currently holds (so concurrent
        builds get distinct tokens), breaking ties by least-recently-handed.
        Falls back to the LRU credential when every distinct one is busy (more
        builds than tokens), or to sharing the only credential when size == 1.
        Returns None only when the pool is empty.

        ``exclude_profile_id`` skips a known-bad profile (failover retry); it is
        ignored if excluding it would leave nothing to hand out.
        """
        with self._lock:
            if not self._entries:
                return None

            # Idempotent: a re-checkout for the same task returns what it holds.
            held = self._checked_out.get(task_id)
            if held is not None and held in self._by_profile:
                return self._by_profile[held].cred

            candidates = [
                e for e in self._entries if e.cred.profile_id != exclude_profile_id
            ]
            if not candidates:
                candidates = list(self._entries)  # excluding all -> ignore exclude

            # Prefer free credentials; among them (or, if none free, among all
            # candidates) pick the least-recently-handed.
            free = [e for e in candidates if e.in_use == 0]
            pickable = free if free else candidates
            chosen = min(pickable, key=lambda e: (e.last_handed, e.in_use))

            self._seq += 1
            chosen.last_handed = self._seq
            chosen.in_use += 1
            self._checked_out[task_id] = chosen.cred.profile_id
            logger.info(
                "[ClaudeTokenPool] checked out profile %s (%s) for task %s "
                "(pool size=%d, in_use=%d)",
                chosen.cred.profile_id,
                chosen.cred.profile_name,
                task_id,
                len(self._entries),
                chosen.in_use,
            )
            return chosen.cred

    def release(self, task_id: str) -> None:
        """Return ``task_id``'s credential to the pool. Idempotent / no-raise."""
        with self._lock:
            profile_id = self._checked_out.pop(task_id, None)
            if profile_id is None:
                return
            entry = self._by_profile.get(profile_id)
            if entry is not None and entry.in_use > 0:
                entry.in_use -= 1
                logger.debug(
                    "[ClaudeTokenPool] released profile %s for task %s (in_use=%d)",
                    profile_id,
                    task_id,
                    entry.in_use,
                )


# ---- credential discovery -------------------------------------------------


def _discover_credentials(
    projects_data_dir: str | os.PathLike[str] | None,
    legacy_profiles_file: Path | None,
) -> list[Credential]:
    """Resolve the list of usable credentials, in source-precedence order."""
    # 1. Explicit env pool — distinct anonymous entries.
    env_pool = os.environ.get(_POOL_ENV_VAR, "").strip()
    if env_pool:
        tokens = [t.strip() for t in _POOL_SPLIT_RE.split(env_pool) if t.strip()]
        if tokens:
            return [
                Credential(
                    token=tok,
                    profile_id=f"env-pool-{i}",
                    profile_name=f"Env Pool {i + 1}",
                )
                for i, tok in enumerate(tokens)
            ]

    # 2. claude-profiles.json — every profile that carries a token.
    profiles_file: Path | None = None
    if projects_data_dir is not None:
        candidate = Path(projects_data_dir) / "claude-profiles.json"
        if candidate.exists():
            profiles_file = candidate
    if profiles_file is None and legacy_profiles_file and legacy_profiles_file.exists():
        profiles_file = legacy_profiles_file

    if profiles_file is not None:
        try:
            data = json.loads(profiles_file.read_text())
            profiles = data.get("profiles", [])
            creds: list[Credential] = []
            seen_tokens: set[str] = set()
            for p in profiles:
                token = p.get("oauthToken") or p.get("token")
                if not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                creds.append(
                    Credential(
                        token=token,
                        profile_id=p.get("id") or f"profile-{len(creds)}",
                        profile_name=p.get("name", "Profile"),
                    )
                )
            if creds:
                return creds
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "[ClaudeTokenPool] failed to load %s: %s", profiles_file, exc
            )

    # 3. Single env-override token.
    single = os.environ.get(_SINGLE_ENV_VAR)
    if single:
        return [
            Credential(
                token=single,
                profile_id="env-override",
                profile_name="Environment Override",
            )
        ]

    # 4. Static fallback file.
    token_file = Path.home() / ".claude" / "oauth_token"
    if token_file.exists():
        try:
            token = token_file.read_text().strip()
            if token:
                return [
                    Credential(
                        token=token,
                        profile_id="static-fallback",
                        profile_name="Static Token",
                    )
                ]
        except OSError as exc:
            logger.warning("[ClaudeTokenPool] failed to read %s: %s", token_file, exc)

    return []
