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


def _default_panes_dir() -> Path:
    """Choose a panes directory that the current process can actually write to.

    Precedence — matches ``wrapper._default_socket_dir`` so socket + FIFOs
    end up on the same filesystem tree:

      1. ``/var/run/aifactory/panes`` if it already exists and is
         writable — this is what the container Helm chart mounts
         (emptyDir owned by the pod user, see design §3.4)
      2. ``$XDG_RUNTIME_DIR/aifactory-rmux/panes`` if XDG is set
         (standard systemd user session path; tmpfs-backed)
      3. ``~/.cache/aifactory/rmux/panes`` as a final portable fallback

    Local dev shells almost never have write access to ``/var/run``,
    so option 1 is essentially production-only.  The previous
    ``Path('/var/run/aifactory/panes')`` constant fell over with
    PermissionError on first ``mkdir`` for local devs running the
    web-server outside a container.
    """
    container_default = Path("/var/run/aifactory/panes")
    if container_default.exists() and os.access(container_default, os.W_OK):
        return container_default
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "aifactory-rmux" / "panes"
    return Path.home() / ".cache" / "aifactory" / "rmux" / "panes"


# Resolved once at module-load time.  ``configure(panes_dir=...)`` lets
# tests + container init override this without monkey-patching.
_DEFAULT_PANES_DIR = _default_panes_dir()


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
    attached_connection_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        self._panes_dir = Path(panes_dir) if panes_dir else _DEFAULT_PANES_DIR
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

            # Bring up rmux + session + pipe-pane.
            #
            # IMPORTANT ordering: register ``_states`` AS SOON AS
            # ``new_session`` returns successfully — BEFORE we attempt
            # ``pipe_pane``.  Earlier the order was new_session →
            # pipe_pane → _states, which meant a transient pipe_pane
            # failure (race against the freshly-spawned daemon's pane
            # registration) would leave an orphaned rmux session AND
            # an empty registry — so the bridge endpoint correctly
            # answered "no session for spec_id" even though the
            # session existed.  Re-ordering keeps the registry
            # consistent with the daemon: if rmux has a session
            # named ``aifactory-task-<spec_id>``, the registry has
            # it too.
            await self._wrapper.ensure_daemon()
            logger.info(
                "[rmux.session] create_for_task: ensure_daemon OK spec_id=%s", spec_id,
            )

            # Defensive: the rmux daemon's session table can outlive a
            # web-server lifetime (we restart the server frequently
            # during dev; the daemon is a separate process and keeps
            # running).  If a session named ``aifactory-task-<spec_id>``
            # is lingering from a previous web-server's task, ``new-session``
            # would fail with "duplicate session" and leave our in-memory
            # ``_states`` empty.  Reap any stale namesake first; the
            # ignore_missing flag makes this a no-op when there's nothing
            # to clean.
            await self._wrapper.kill_session(session_name, ignore_missing=True)
            await self._wrapper.new_session(
                session_name, worktree_path, agent_cmd
            )
            logger.info(
                "[rmux.session] create_for_task: new_session OK spec_id=%s session=%s",
                spec_id, session_name,
            )

            # Register BEFORE pipe_pane so the WS can find us even if
            # pipe-pane attaches late (the FIFO simply has no bytes
            # until then, but the bridge can still resolve the session).
            self._states[spec_id] = SessionState(
                spec_id=spec_id,
                session_name=session_name,
                fifo_path=fifo_path,
            )
            logger.info(
                "[rmux.session] create_for_task: registered in _states spec_id=%s (states count=%d)",
                spec_id, len(self._states),
            )

            try:
                await self._wrapper.pipe_pane(session_name, fifo_path)
                logger.info(
                    "[rmux.session] create_for_task: pipe_pane OK spec_id=%s", spec_id,
                )
            except Exception as e:
                # Don't unregister — caller can still see "session
                # exists but no bytes".  Log so the operator notices.
                logger.warning(
                    "[rmux.session] create_for_task: pipe_pane FAILED spec_id=%s err=%s — session still registered",
                    spec_id, e,
                )

            return fifo_path

    async def reap_for_task(self, spec_id: str) -> None:
        """Kill the session + remove the FIFO.  Idempotent.

        Called from ``agent_service`` on task completion/failure/discard.
        Logs but never raises — reaping must not block task shutdown.
        """
        async with self._registry_lock:
            state = self._states.pop(spec_id, None)
            if state is None:
                return  # nothing to reap

        # Outside the registry lock — these are slow-ish subprocess ops
        # and other callers don't need to wait on them.
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
