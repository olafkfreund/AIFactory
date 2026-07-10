"""Claude credential / token-pool / profile-switch / retry — CredentialMixin (#703).

The credential-resolution, token-pool checkout, rate-limit/early-failure
detection, active-profile switching and task-retry methods, lifted out of the
AgentService god-class into a mixin (see #703 / #704 for the pattern). AgentService
inherits this mixin; methods run as bound methods on the instance via the MRO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)


class CredentialMixin:
    """Credential / token-pool / profile-switch / retry methods for AgentService."""

    if TYPE_CHECKING:
        # Attributes provided by the concrete host (AgentService); declared here
        # so mypy can resolve the self.* references in a mixin (#703 pattern).
        settings: Any
        _task_profiles: dict[str, Any]
        _token_pool: Any
        _token_pool_build_lock: Any

    def _resolve_profiles_file(self) -> Path:
        """Resolve claude-profiles.json, preferring the primary data dir and
        falling back to the legacy data-dir location when only that exists."""
        from ..paths import get_data_file

        profiles_file = Path(self.settings.PROJECTS_DATA_DIR) / "claude-profiles.json"
        legacy_profiles_file = get_data_file("claude-profiles.json")
        if not profiles_file.exists() and legacy_profiles_file.exists():
            profiles_file = legacy_profiles_file
            _log.debug(
                f"[AgentService] Using legacy profiles file at {profiles_file}"
            )
        return profiles_file

    def _resolve_claude_token(
        self, exclude_profile_id: str | None = None
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve Claude OAuth token from profiles with fallback chain.

        Resolution order:
        1. Environment override (CLAUDE_CODE_OAUTH_TOKEN already set)
        2. Active profile from ~/.aifactory/claude-profiles.json
        3. Best available profile (excluding failed profile if provided)
        4. Fallback to ~/.claude/oauth_token

        Args:
            exclude_profile_id: Profile ID to exclude (for retry after failure)

        Returns:
            Tuple of (token, profile_id, profile_name) or (None, None, None) if no token found
        """
        import logging

        logger = logging.getLogger(__name__)

        # Check environment override first
        if "CLAUDE_CODE_OAUTH_TOKEN" in os.environ:
            # Allow failover when this "env-override" profile is excluded.
            if exclude_profile_id != "env-override":
                logger.info(
                    "[AgentService] Using CLAUDE_CODE_OAUTH_TOKEN from environment"
                )
                return (
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
                    "env-override",
                    "Environment Override",
                )
            logger.info(
                "[AgentService] Skipping environment token due to exclude_profile_id=env-override (failover enabled)"
            )

        # Load claude-profiles.json
        profiles_file = self._resolve_profiles_file()

        if profiles_file.exists():
            try:
                data = json.loads(profiles_file.read_text())
                profiles = data.get("profiles", [])
                active_id = data.get("activeProfileId")

                # Filter usable profiles (has token, not excluded)
                usable = [
                    p
                    for p in profiles
                    if p.get("id") != exclude_profile_id
                    and (
                        p.get("oauthToken") or p.get("token")
                    )  # Support both field names
                ]

                if usable:
                    # Prefer active profile if it's usable
                    for p in usable:
                        if p.get("id") == active_id:
                            token = p.get("oauthToken") or p.get("token")
                            profile_id = p.get("id")
                            profile_name = p.get("name", "Active Profile")
                            logger.info(
                                f"[AgentService] Using active profile: {profile_name} ({profile_id})"
                            )
                            return (token, profile_id, profile_name)

                    # Use first usable profile
                    p = usable[0]
                    token = p.get("oauthToken") or p.get("token")
                    profile_id = p.get("id")
                    profile_name = p.get("name", "Default Profile")
                    logger.info(
                        f"[AgentService] Using profile: {profile_name} ({profile_id})"
                    )
                    return (token, profile_id, profile_name)

            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    f"[AgentService] Failed to load claude-profiles.json: {e}"
                )

        # Fallback to static token file
        token_file = Path.home() / ".claude" / "oauth_token"
        if token_file.exists():
            token = token_file.read_text().strip()
            logger.info(
                "[AgentService] Using fallback token from ~/.claude/oauth_token"
            )
            return (token, "static-fallback", "Static Token")

        logger.warning("[AgentService] No Claude token found")
        return (None, None, None)

    def _get_token_pool(self) -> Any:
        """Lazily build the Claude token pool (RFC-0016 #670).

        Double-checked locking: concurrent first-checkouts must share ONE pool,
        else each thread would build a private pool and they'd all hand out the
        same LRU credential.
        """
        if self._token_pool is None:
            with self._token_pool_build_lock:
                if self._token_pool is None:
                    from ..paths import get_data_file
                    from .claude_token_pool import ClaudeTokenPool

                    self._token_pool = ClaudeTokenPool.from_sources(
                        self.settings.PROJECTS_DATA_DIR,
                        legacy_profiles_file=get_data_file("claude-profiles.json"),
                    )
                    _log.info(
                        "[AgentService] Claude token pool built with %d distinct "
                        "credential(s)",
                        self._token_pool.size,
                    )
        return self._token_pool

    def reset_token_pool(self) -> None:
        """Drop the cached token pool so the next build rebuilds it from source.

        The pool (``_get_token_pool``) is cached for the process lifetime, so a
        Claude-profile change made via the portal (Settings → Claude Profiles)
        would otherwise not reach a warmed pool until a pod restart. Calling this
        after a profile mutation makes the new token take effect on the next
        build — no restart. Cheap (the next checkout rebuilds lazily).
        """
        with self._token_pool_build_lock:
            self._token_pool = None

    def _resolve_claude_token_pooled(
        self, task_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Check a DISTINCT credential out of the pool for ``task_id``.

        Concurrent builds get distinct tokens when several are configured; the
        single shared token otherwise (identical to the legacy single-token
        behaviour). The env-override token (CLAUDE_CODE_OAUTH_TOKEN) keeps its
        legacy precedence — but is also poolable when multiple are configured.

        Falls back to the legacy resolver if the pool is empty (e.g. a token
        source the pool can't see). The checked-out credential is returned to
        the pool by :meth:`_release_task_credential` when the build ends.
        """
        try:
            pool = self._get_token_pool()
            if not pool.is_empty():
                cred = pool.checkout(task_id)
                if cred is not None:
                    return (cred.token, cred.profile_id, cred.profile_name)
        except Exception:  # noqa: BLE001 - pool must never break a build start
            _log.warning(
                "[AgentService] token pool checkout failed; falling back to "
                "single-token resolver",
                exc_info=True,
            )
        # Fallback: legacy single-token resolution (no pool tracking to release).
        return self._resolve_claude_token()

    def _release_task_credential(self, task_id: str) -> None:
        """Return a build's pooled credential when it ends. Idempotent/no-raise.

        Also pops ``_task_profiles[task_id]`` so this can stand in for the bare
        ``_task_profiles.pop`` calls at every task-terminal site.
        """
        try:
            if self._token_pool is not None:
                self._token_pool.release(task_id)
        except Exception:  # noqa: BLE001
            _log.debug(
                "[AgentService] token pool release raised for %s (ignored)",
                task_id,
                exc_info=True,
            )
        self._task_profiles.pop(task_id, None)

    def _is_early_failure(self, spec_dir: Path, exit_code: int) -> bool:
        """Check if task failure is an early failure (no logs written).

        Early failure criteria:
        - Exit code is non-zero
        - task_logs.json either doesn't exist OR has no entries in any phase

        This indicates the agent failed immediately without making progress,
        typically due to auth/rate-limit issues.

        Args:
            spec_dir: Path to the spec directory containing task_logs.json
            exit_code: Process exit code

        Returns:
            True if this is an early failure eligible for retry
        """
        if exit_code == 0:
            return False

        task_logs_file = spec_dir / "task_logs.json"

        # If file doesn't exist, it's an early failure
        if not task_logs_file.exists():
            return True

        try:
            data = json.loads(task_logs_file.read_text())
            phases = data.get("phases", {})

            # Check if any phase has entries
            for phase_name, phase_data in phases.items():
                entries = phase_data.get("entries", [])
                if entries:
                    # Found entries - this is NOT an early failure
                    return False

            # No entries in any phase - early failure
            return True

        except (json.JSONDecodeError, OSError):
            # Can't read logs - assume early failure to be safe
            return True

    def _should_retry_with_failover(self) -> bool:
        """Check if auto-switch settings allow profile failover.

        Checks:
        - enabled: Master switch for auto-switching
        - autoSwitchOnRateLimit: Reactive recovery toggle

        Returns:
            True if both settings are enabled
        """
        import logging

        logger = logging.getLogger(__name__)

        # Primary path: ~/.aifactory/auto-switch.json
        settings_file = Path(self.settings.PROJECTS_DATA_DIR) / "auto-switch.json"

        if not settings_file.exists():
            logger.debug(
                f"[AgentService] Auto-switch settings not found at {settings_file}, failover disabled"
            )
            return False

        try:
            data = json.loads(settings_file.read_text())
            enabled = data.get("enabled", False)
            auto_switch_on_rate_limit = data.get("autoSwitchOnRateLimit", False)

            if enabled and auto_switch_on_rate_limit:
                logger.info("[AgentService] Auto-switch enabled - failover allowed")
                return True
            else:
                logger.debug(
                    f"[AgentService] Auto-switch disabled - enabled: {enabled}, autoSwitchOnRateLimit: {auto_switch_on_rate_limit}"
                )
                return False

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[AgentService] Failed to read auto-switch settings: {e}")
            return False

    def _is_rate_limit_line(self, line: str) -> bool:
        """Detect rate limit messages in agent output."""
        text = line.lower()
        patterns = [
            "you've hit your limit",
            "you’ve hit your limit",  # curly apostrophe
            "youve hit your limit",
        ]
        return any(p in text for p in patterns)

    async def _emit_profile_switch(
        self,
        task_id: str,
        old_profile_id: str,
        new_profile_id: str,
        new_profile_name: str,
        reason: str,
    ) -> None:
        """Emit profile switch event via WebSocket.

        Args:
            task_id: Task identifier
            old_profile_id: Previous profile ID that failed
            new_profile_id: New profile ID being used
            new_profile_name: New profile display name
            reason: Reason for switch (e.g., "early_failure")
        """
        from ..websockets.events import broadcast_event

        await broadcast_event(
            "task:profile-switch",
            {
                "taskId": task_id,
                "oldProfileId": old_profile_id,
                "newProfileId": new_profile_id,
                "newProfileName": new_profile_name,
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
            },
        )

    def _update_active_profile(
        self, profile_id: str, profile_name: str, reason: str = "rate_limit"
    ) -> None:
        """Update active profile system-wide when reactive failover occurs.

        This updates the activeProfileId in claude-profiles.json so that all future
        tasks automatically use the new profile instead of repeatedly failing.

        Args:
            profile_id: ID of new profile to make active
            profile_name: Name for logging
            reason: Why the switch occurred (e.g., "rate_limit", "reactive_failover")
        """
        import logging

        logger = logging.getLogger(__name__)

        profiles_file = self._resolve_profiles_file()

        if not profiles_file.exists():
            logger.warning(
                "[AgentService] claude-profiles.json not found, skipping active profile update"
            )
            return

        try:
            # Read current profiles
            data = json.loads(profiles_file.read_text())
            old_active = data.get("activeProfileId")

            # Update active profile
            data["activeProfileId"] = profile_id

            # Write back with secure permissions
            profiles_file.write_text(json.dumps(data, indent=2))
            profiles_file.chmod(0o600)

            # Update env token to match active profile (if available)
            token = None
            for profile in data.get("profiles", []):
                if profile.get("id") == profile_id:
                    token = profile.get("oauthToken") or profile.get("token")
                    break

            if token:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
                logger.info(
                    "[AgentService] Updated CLAUDE_CODE_OAUTH_TOKEN for active profile"
                )
            else:
                logger.warning(
                    "[AgentService] Active profile has no token; env not updated"
                )

            logger.info(
                f"[AgentService] Updated active profile: {old_active} → {profile_id} (reason: {reason})"
            )

            # Emit WebSocket event for system-wide profile change
            from ..websockets.events import broadcast_event

            asyncio.create_task(
                broadcast_event(
                    "profile:changed",
                    {
                        "oldProfileId": old_active,
                        "newProfileId": profile_id,
                        "newProfileName": profile_name,
                        "reason": reason,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
            )

        except Exception as e:
            logger.error(f"[AgentService] Failed to update active profile: {e}")

    async def _retry_task_with_fallback_model(
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        cmd: list[str],
        env: dict,
    ) -> asyncio.subprocess.Process | None:
        """Retry task execution with Claude Sonnet as fallback model.

        Called when a non-Claude model (Codex, Gemini, Ollama) fails.
        Swaps the --model flag in the command to 'sonnet'.

        Returns:
            New subprocess or None if retry not possible
        """
        import logging

        logger = logging.getLogger(__name__)

        profile_info = self._task_profiles.get(task_id, {})
        failed_model = profile_info.get("model", "unknown")

        # Build new command with sonnet model
        new_cmd = list(cmd)
        if "--model" in new_cmd:
            model_idx = new_cmd.index("--model")
            if model_idx + 1 < len(new_cmd):
                new_cmd[model_idx + 1] = "sonnet"
        else:
            new_cmd.extend(["--model", "sonnet"])

        logger.info(
            f"[AgentService] [Model: sonnet] Fallback triggered for {task_id} (original: {failed_model})"
        )

        # Emit WebSocket event for model fallback
        from ..websockets.events import broadcast_event

        await broadcast_event(
            "task:log",
            {
                "taskId": task_id,
                "type": "model_fallback",
                "message": f"Model '{failed_model}' failed. Falling back to Claude Sonnet.",
            },
        )

        # Update tracking
        if task_id in self._task_profiles:
            self._task_profiles[task_id]["model"] = "sonnet"
            self._task_profiles[task_id]["attempt"] = 2
            self._task_profiles[task_id]["fallbackFrom"] = failed_model

        # Relaunch subprocess
        import pty

        master_fd, slave_fd = pty.openpty()

        proc = await asyncio.create_subprocess_exec(
            *new_cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            env=env,
        )

        os.close(slave_fd)
        os.close(master_fd)

        return proc

    async def _retry_task_with_profile(
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        cmd: list[str],
        env: dict,
        failed_profile_id: str,
        reason: str,
    ) -> asyncio.subprocess.Process | None:
        """Retry task execution with a different Claude profile.

        Args:
            task_id: Task identifier
            project_path: Project directory
            spec_id: Spec identifier
            cmd: Command to execute (same as original)
            env: Environment dict (will update token)
            failed_profile_id: Profile ID that failed (to exclude)

        Returns:
            New subprocess or None if retry not possible
        """
        import logging

        logger = logging.getLogger(__name__)

        # Resolve alternate token (excluding failed profile)
        token, profile_id, profile_name = self._resolve_claude_token(
            exclude_profile_id=failed_profile_id
        )

        if not token:
            logger.warning(
                f"[AgentService] No alternate profile available for retry (excluded: {failed_profile_id})"
            )
            return None

        if profile_id == failed_profile_id:
            logger.warning(
                f"[AgentService] Only profile available is the one that failed ({failed_profile_id})"
            )
            return None

        # Update environment with new token
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        # Log profile switch
        logger.info(
            f"[AgentService] Retrying with profile: {profile_name} ({profile_id})"
        )

        # Emit WebSocket event for profile switch
        await self._emit_profile_switch(
            task_id=task_id,
            old_profile_id=failed_profile_id,
            new_profile_id=profile_id,
            new_profile_name=profile_name,
            reason=reason,
        )

        # Update active profile system-wide (only for rate limit, not early failure)
        if reason == "rate_limit":
            self._update_active_profile(
                profile_id, profile_name, reason="reactive_failover"
            )

        # Update tracking
        if task_id in self._task_profiles:
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 2,  # Second attempt
                "previousProfileId": failed_profile_id,
            }

        # Relaunch subprocess with new token
        import pty

        master_fd, slave_fd = pty.openpty()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            env=env,
        )

        os.close(slave_fd)

        return proc
