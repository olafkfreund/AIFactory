"""Per-task rmux session lifecycle (Epic #44, issue #46).

Maps an AIFactory ``spec_id`` to:

  - an rmux session named ``aifactory-task-<spec_id>``
  - a Unix FIFO at ``<panes_dir>/<spec_id>.fifo`` that pipe-pane writes
    bytes to as the agent produces output
  - per-session mutable state needed by the WebSocket bridge:
    an ``asyncio.Lock`` to serialise attach mode flips, and the
    currently-attached ``connection_id`` (or ``None`` when read-only).

Module-level singleton.  ``agent_service`` calls ``create_for_task``
when a task starts (only when ``AIFACTORY_RMUX_ENABLED=true``) and
``reap_for_task`` when it ends.

Threading model
---------------

Everything runs on a single asyncio event loop in the web-server
process.  No threads, no multi-process — multi-replica rmux is
explicitly out of scope for v1 (design §3.4 pins ``replicas: 1`` in
the Helm chart when the feature is enabled).  The ``asyncio.Lock``s
serialise (a) registry mutations against concurrent create/reap calls
for the same ``spec_id``, and (b) attach-mode flips so a 1000-concurrent
``POST /attach`` race resolves to exactly one 200 + 999 409s
(acceptance criterion in design §7).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .wrapper import RmuxError, RmuxWrapper

logger = logging.getLogger(__name__)

# Default panes directory. In a container ``/var/run/aifactory/panes`` is
# writable, but on a local laptop it isn't — so we resolve a writable
# default at runtime (env override → data dir → /var/run as last resort).
# Overridden for tests via ``configure(panes_dir=...)`` (tmp_path).
def _default_panes_dir() -> Path:
    env = os.environ.get("AIFACTORY_RMUX_PANES_DIR", "").strip()
    if env:
        return Path(env)
    try:
        from ..paths import get_data_dir

        return get_data_dir() / "panes"
    except Exception:
        return Path("/var/run/aifactory/panes")


@dataclass
class SessionState:
    """Per-task mutable state held in the registry.

    The ``lock`` here protects ``attached_connection_id`` against a
    1000-concurrent ``POST /attach`` race.  Any handler that wants to
    flip attach mode MUST acquire this lock first.
    """

    spec_id: str
    session_name: str
    fifo_path: Path
    # Owning project id, used to authorize console attach/stream (#322). None
    # only for legacy sessions created before this was threaded through — the
    # bridge treats None as "service-principal only".
    project_id: str | None = None
    attached_connection_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Non-blocking write fd into the FIFO, lazily opened by ``feed`` once a
    # reader (the WS bridge) is connected. ``None`` when no writer is open.
    write_fd: int | None = None
    # True for sessions fed by agent_service's existing PTY (no rmux process
    # spawned). Read-only streaming works; Attach/send-keys is unavailable.
    passive: bool = False


class SessionRegistry:
    """Module-singleton registry mapping ``spec_id`` → ``SessionState``.

    Constructor parameters are exposed for tests (point at a tmp_path
    panes dir, inject a wrapper bound to a tmp-path socket).  Production
    uses defaults.
    """

    def __init__(
        self,
        wrapper: RmuxWrapper | None = None,
        panes_dir: Path | str | None = None,
    ) -> None:
        self._wrapper = wrapper or RmuxWrapper()
        self._panes_dir = Path(panes_dir) if panes_dir else _default_panes_dir()
        self._states: dict[str, SessionState] = {}
        # Serialises mutations to ``_states`` itself.  Per-session
        # ``attached_connection_id`` flips use the per-state lock.
        self._registry_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_for_task(
        self,
        spec_id: str,
        worktree_path: str | Path,
        agent_cmd: str | list[str],
        project_id: str | None = None,
    ) -> Path:
        """Spin up the rmux session + FIFO + pipe-pane for ``spec_id``.

        Returns the FIFO path the bridge layer reads bytes from.

        Raises:
            ValueError: if a session for ``spec_id`` already exists
                (caller must reap before re-create).
            RmuxError: rmux subprocess failures bubble up unchanged so
                ``agent_service`` can fall back to the existing PTY
                path and surface a banner per design §6.

        Note on ordering: ``new_session`` actually starts the agent
        command, and rmux returns immediately (it's ``-d`` detached).
        ``pipe-pane`` runs right after; in practice the agent's first
        bytes don't land until after this returns because subprocess
        startup is slower than the wrapper round-trip.  But the
        contract is "pipe-pane attaches eagerly" — see R0a gotcha #2.
        """
        session_name = f"aifactory-task-{spec_id}"
        fifo_path = self._panes_dir / f"{spec_id}.fifo"

        async with self._registry_lock:
            if spec_id in self._states:
                raise ValueError(
                    f"rmux session already exists for spec_id={spec_id!r}"
                )

            # Create panes dir + FIFO.  mkfifo blows up if the path
            # already exists, so unlink first (idempotent recovery
            # from a half-cleaned previous run).
            self._panes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if fifo_path.exists():
                fifo_path.unlink()
            os.mkfifo(str(fifo_path), mode=0o600)

            # Bring up rmux + session + pipe-pane in one shot.
            await self._wrapper.ensure_daemon()
            await self._wrapper.new_session(
                session_name, worktree_path, agent_cmd
            )
            await self._wrapper.pipe_pane(session_name, fifo_path)

            state = SessionState(
                spec_id=spec_id,
                session_name=session_name,
                fifo_path=fifo_path,
                project_id=project_id,
            )
            self._states[spec_id] = state
            logger.info(
                "rmux session created: spec_id=%s session=%s fifo=%s",
                spec_id, session_name, fifo_path,
            )
        # Mirror into the shared Redis panes index OUTSIDE the registry lock
        # (#681) — best-effort, no-op when Redis is off.
        await self._register_pane_in_redis(state)
        return fifo_path

    async def create_passive_for_task(
        self, spec_id: str, project_id: str | None = None
    ) -> Path:
        """Register a FIFO-only session WITHOUT spawning an rmux process.

        Used when the agent already runs under agent_service's own PTY:
        rmux ``new-session`` would double-spawn the agent, so instead we
        just create the FIFO + registry state and let agent_service
        ``feed`` the agent's output bytes into it. The WS bridge streams
        read-only exactly as it does for a real rmux pane. Attach/send-keys
        is unavailable for passive sessions (no rmux pane to target).

        Returns the FIFO path. Raises ValueError if already registered.
        """
        session_name = f"aifactory-task-{spec_id}"
        fifo_path = self._panes_dir / f"{spec_id}.fifo"

        async with self._registry_lock:
            if spec_id in self._states:
                raise ValueError(
                    f"rmux session already exists for spec_id={spec_id!r}"
                )
            self._panes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if fifo_path.exists():
                fifo_path.unlink()
            os.mkfifo(str(fifo_path), mode=0o600)
            state = SessionState(
                spec_id=spec_id,
                session_name=session_name,
                fifo_path=fifo_path,
                project_id=project_id,
                passive=True,
            )
            self._states[spec_id] = state
            logger.info(
                "rmux passive session created: spec_id=%s fifo=%s",
                spec_id, fifo_path,
            )
        # Mirror into the shared Redis panes index OUTSIDE the registry lock
        # (#681) — best-effort, no-op when Redis is off.
        await self._register_pane_in_redis(state)
        return fifo_path

    def feed(self, spec_id: str, data: bytes) -> None:
        """Best-effort write of agent output bytes into a passive FIFO.

        Lazily opens the FIFO write end non-blocking — which only succeeds
        while a reader (the WS bridge) is connected, giving natural
        "live tail" semantics: bytes are delivered to whoever is watching,
        and silently dropped when nobody is. Never raises.

        RFC-0017 #681: when a shared Redis bus is configured (``REDIS_URL``) the
        same bytes are ALSO published to the session's Redis channel so a WS
        bridge on ANY replica can stream this session — not just the pod hosting
        the FIFO. No-op + no behaviour change when Redis is off.
        """
        state = self._states.get(spec_id)
        if state is None or not state.passive or not data:
            return
        self._publish_pane_bytes_to_redis(spec_id, data)
        try:
            if state.write_fd is None:
                try:
                    state.write_fd = os.open(
                        str(state.fifo_path), os.O_WRONLY | os.O_NONBLOCK
                    )
                except OSError:
                    # ENXIO = no reader connected yet; drop until one is.
                    return
            os.write(state.write_fd, data)
        except (BlockingIOError, InterruptedError):
            # Pipe full (slow viewer) — drop this chunk, keep the fd.
            pass
        except OSError:
            # Reader went away (EPIPE) — reset so we re-open on next viewer.
            try:
                if state.write_fd is not None:
                    os.close(state.write_fd)
            except OSError:
                pass
            state.write_fd = None

    @staticmethod
    def _publish_pane_bytes_to_redis(spec_id: str, data: bytes) -> None:
        """Fire-and-forget publish of pane bytes onto the shared Redis bus (#681).

        ``feed`` is sync (called from the output-processing loop), so schedule
        the async publish on the running loop without blocking. No-op when Redis
        is off or no loop is running. Never raises — console fan-out must never
        affect task execution.
        """
        try:
            from . import redis_transport

            if not redis_transport.redis_enabled():
                return
            loop = asyncio.get_running_loop()
            loop.create_task(redis_transport.publish_pane_bytes(spec_id, data))
        except RuntimeError:
            # No running loop (e.g. sync test context) — skip the Redis mirror.
            pass
        except Exception:  # noqa: BLE001 - Redis fan-out is best-effort
            logger.debug(
                "[rmux] redis pane publish scheduling failed for %s",
                spec_id, exc_info=True,
            )

    async def _register_pane_in_redis(self, state: SessionState) -> None:
        """Mirror a session into the shared Redis panes index (#681). No-op off."""
        try:
            from . import redis_transport

            await redis_transport.register_pane(
                state.spec_id,
                {
                    "spec_id": state.spec_id,
                    "session_name": state.session_name,
                    "project_id": state.project_id,
                    "passive": state.passive,
                },
            )
        except Exception:  # noqa: BLE001 - index mirror is best-effort
            logger.debug(
                "[rmux] redis register_pane failed for %s",
                state.spec_id, exc_info=True,
            )

    @staticmethod
    async def _unregister_pane_in_redis(spec_id: str) -> None:
        """Remove a session from the shared Redis panes index (#681). No-op off."""
        try:
            from . import redis_transport

            await redis_transport.unregister_pane(spec_id)
        except Exception:  # noqa: BLE001 - index mirror is best-effort
            logger.debug(
                "[rmux] redis unregister_pane failed for %s", spec_id, exc_info=True
            )

    async def reap_for_task(self, spec_id: str) -> None:
        """Kill the session + remove the FIFO.  Idempotent.

        Called from ``agent_service`` on task completion/failure/discard.
        Logs but never raises — reaping must not block task shutdown.
        """
        async with self._registry_lock:
            state = self._states.pop(spec_id, None)
            if state is None:
                # Still drop a stale shared-index entry — a session this pod
                # never hosted may have been registered by another replica (#681).
                await self._unregister_pane_in_redis(spec_id)
                return  # nothing else to reap locally

        # Drop from the shared Redis panes index (#681) — best-effort, no-op off.
        await self._unregister_pane_in_redis(spec_id)

        # Close any open FIFO writer (passive sessions).
        if state.write_fd is not None:
            try:
                os.close(state.write_fd)
            except OSError:
                pass
            state.write_fd = None

        # Outside the registry lock — these are slow-ish subprocess ops
        # and other callers don't need to wait on them. Passive sessions
        # have no rmux process, so skip kill-session for them.
        if not state.passive:
            try:
                await self._wrapper.kill_session(
                    state.session_name, ignore_missing=True
                )
            except RmuxError:
                logger.warning(
                    "rmux kill-session failed during reap (ignored): %s",
                    state.session_name,
                    exc_info=True,
                )
        try:
            state.fifo_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "fifo unlink failed during reap (ignored): %s",
                state.fifo_path,
                exc_info=True,
            )

        logger.info(
            "rmux session reaped: spec_id=%s session=%s",
            spec_id, state.session_name,
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_state(self, spec_id: str) -> SessionState | None:
        """Return the state for ``spec_id`` or ``None`` if not registered."""
        return self._states.get(spec_id)

    @property
    def wrapper(self) -> RmuxWrapper:
        """Expose the wrapper for the bridge layer (send-keys forwarding)."""
        return self._wrapper

    def __iter__(self) -> Iterator[str]:
        """Iterate over currently-registered spec_ids (for diagnostics)."""
        return iter(self._states.keys())


# ---------------------------------------------------------------------------
# Module-level singleton + configuration hook
# ---------------------------------------------------------------------------


_registry: SessionRegistry | None = None


def get_registry() -> SessionRegistry:
    """Return the module-level singleton, lazily creating it on first call."""
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry


def configure(
    *,
    wrapper: RmuxWrapper | None = None,
    panes_dir: Path | str | None = None,
) -> SessionRegistry:
    """Replace the singleton with one bound to test/container settings.

    Production should call this exactly once at web-server startup
    (e.g. from a FastAPI startup hook gated by
    ``AIFACTORY_RMUX_ENABLED``).  Tests call it in fixtures to point
    at a tmp_path FIFO directory + a wrapper using a tmp-path socket.
    """
    global _registry
    _registry = SessionRegistry(wrapper=wrapper, panes_dir=panes_dir)
    return _registry


def reset_for_tests() -> None:
    """Drop the singleton — convenience for test teardown.

    The next ``get_registry()`` call will create a fresh default
    registry.  Tests that use ``configure()`` should call this in
    teardown so they don't leak state across the suite.
    """
    global _registry
    _registry = None
