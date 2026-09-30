"""RFC-0017 #680 — Job-native log streaming for the kubejob build backend.

The in-pod subprocess build path (``AgentService._process_output``) streams a
build's live output to two sinks:

  * the **cockpit log stream** — one ``TaskLog`` per line, fanned out to the
    task-detail WebSocket via ``AgentService._emit_log``;
  * the **rmux Live Agent Console** — raw bytes tee'd into the task's pane FIFO
    via ``rmux.integration.feed_if_enabled``.

When ``AIFACTORY_BUILD_BACKEND=kubejob`` (RFC-0016 #671) the build runs as a
per-task Kubernetes Job instead of an in-pod subprocess, so neither sink was
fed — which is the only reason kubejob isn't the default yet (RFC-0017 §2.1).

This module closes that gap. ``KubeJobLogStreamer`` follows the build Job pod's
logs (``kubectl logs -f`` equivalent via the k8s API the ``kube_sandbox``
already uses) and pumps each line into the **same two sinks** the in-pod path
feeds. The in-pod path is untouched.

Design:

* **Best-effort, never load-bearing.** Logs are observability, not correctness.
  Every failure (pod not found yet, stream drop, sink raising) is swallowed and
  logged at DEBUG/WARNING; the build + reconcile complete regardless. A streamer
  crash never strands or fails a build.
* **Unit-testable without a cluster.** Cluster I/O is confined to one injectable
  line-source (``line_source``); tests pass a fake async iterator of lines and
  assert they reach both (fake) sinks. The pure pump loop + sink fan-out are
  exercised against fakes — no real k8s, no real rmux FIFO.
* **Two sinks, mirror semantics.** rmux gets raw bytes with ``\\n`` → ``\\r\\n``
  (xterm needs CRLF, identical to ``_process_output``); the cockpit gets a
  decoded line via the injected ``log_sink`` coroutine.

One thing here is NOT observability. The stream is also the control plane's
clock for pulling the Job's ``implementation_plan.json`` while the build runs
(``plan_sync``, #1228) — the file carrying per-subtask status, written inside
the Job's ephemeral ``/work`` and previously pulled only at completion, which
is why CFactory's execution DAG showed every node waiting for a whole build.
It rides here because this is the one control-plane task whose lifetime is
exactly the build's; it is throttled, and still best-effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

_log = logging.getLogger(__name__)

# How long to keep retrying to locate the build Job's pod before giving up on
# log streaming for this build. The pod is usually schedulable within seconds;
# this is a generous ceiling so a slow scheduler doesn't lose the whole stream.
_POD_WAIT_ATTEMPTS = 40
_POD_WAIT_INTERVAL_SECONDS = 3.0

# Sink callable types. ``LogLineSink`` receives one decoded line (cockpit);
# ``RmuxFeed`` receives raw bytes for the pane FIFO.
LogLineSink = Callable[[str], Awaitable[None]]
RmuxFeed = Callable[[str, bytes], None]

# Factory producing an async iterator of raw log-line bytes for (namespace,
# job_name). Injectable so tests bypass the cluster; the default follows the
# Job pod's logs via the k8s API.
LineSource = Callable[[str, str], AsyncIterator[bytes]]

# #1619: "is this Job still running?", asked after a clean end-of-stream to
# decide reattach-vs-stop. Injectable so tests need no cluster; when absent the
# streamer makes a single pass, which is the pre-#1619 behaviour.
JobActive = Callable[[], Awaitable[bool]]

# Pulls the Job's pushed ``implementation_plan.json`` onto the control plane
# (#1228). Blocking object-store I/O, so it is run off the event loop.
PlanSync = Callable[[], Any]

# How often the plan is pulled while a build runs. The plan is small and the
# pull is one object-store GET, but it rides a per-LINE hook — a busy build
# emits thousands of lines a minute, so without a throttle this would be a GET
# per line. Ten seconds is well under the cockpit's own poll cadence, so the DAG
# is never more than one interval behind the Job.
_PLAN_SYNC_INTERVAL_SECONDS = 10.0

# #1619: how long to wait before re-following a Job's log after a clean EOF,
# and how many consecutive empty reattaches to tolerate before giving up.
# 3s x 20 is a minute of silence — long enough to ride out a pod restart or an
# API blip, short enough that a genuinely dead stream says so while the build
# is still running rather than at the end.
_REATTACH_INTERVAL_SECONDS = 3.0
_MAX_EMPTY_REATTACHES = 20


async def _default_line_source(namespace: str, job_name: str) -> AsyncIterator[bytes]:
    """Follow the build Job pod's logs via the k8s API, yielding line bytes.

    Mirrors ``kube_sandbox`` config loading + pod lookup: locate the Job's pod
    by the ``job-name`` label, then stream its log with ``follow=True`` and
    ``_preload_content=False`` so the response is an aiohttp stream we read
    line-by-line (the ``kubectl logs -f`` equivalent). Yields nothing — rather
    than raising — when the pod never appears or the stream cannot be opened, so
    the caller degrades to "no live logs" instead of failing.
    """
    from kubernetes_asyncio import client, config

    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001 - dev/test fallback to kubeconfig
        await config.load_kube_config()

    api = client.ApiClient()
    core = client.CoreV1Api(api)
    try:
        pod_name = await _await_pod_name(core, namespace, job_name)
        if pod_name is None:
            _log.warning(
                "[build_log_stream] no pod for Job %s/%s appeared — "
                "skipping Job-native log stream",
                namespace,
                job_name,
            )
            return

        # With ``_preload_content=False`` kubernetes_asyncio returns an aiohttp
        # ClientResponse (NOT the ``str`` the stubs claim); its ``.content`` is
        # an async iterator yielding the raw bytes of each log line.
        resp: Any = await core.read_namespaced_pod_log(
            pod_name,
            namespace,
            follow=True,
            _preload_content=False,
        )
        async for line in resp.content:
            yield line
    finally:
        await api.close()


async def _await_pod_name(core: Any, namespace: str, job_name: str) -> str | None:
    """Poll for the Job's pod name (by ``job-name`` label), or None on timeout.

    The pod is created by the Job controller shortly after the Job applies; we
    retry a bounded number of times rather than racing the scheduler.
    """
    for _ in range(_POD_WAIT_ATTEMPTS):
        try:
            pods = await core.list_namespaced_pod(
                namespace, label_selector=f"job-name={job_name}"
            )
        except Exception:  # noqa: BLE001 - transient API error; retry
            _log.debug(
                "[build_log_stream] pod list for %s/%s raised (retrying)",
                namespace,
                job_name,
                exc_info=True,
            )
            pods = None
        items = getattr(pods, "items", None) or []
        if items and _pod_has_started(items[0]):
            name = items[0].metadata.name
            if name:
                return str(name)
        await asyncio.sleep(_POD_WAIT_INTERVAL_SECONDS)
    return None


def _pod_has_started(pod: Any) -> bool:
    """True once the pod's app container is running (or already finished).

    #1619: waiting only for the pod *object* was the whole defect. The pod is
    created seconds after the Job applies, but a build pod runs three init
    containers first — the npm install in ``install-clis`` alone takes over a
    minute. Following the log of a container that has not started returns an
    immediately-ending stream, and the streamer treated that clean EOF as
    "the build produced one line and finished".

    Measured on task 022: pod created 09:22:35, stream "completed (1 line(s))"
    09:22:37, app container started 09:24:03 — 86 seconds later, after which
    145 minutes of phase events went nowhere.

    ``phase`` is the cheapest correct predicate: Pending covers init, and
    Running/Succeeded/Failed all mean the app containers have been started.
    """
    phase = getattr(getattr(pod, "status", None), "phase", None)
    return str(phase) in {"Running", "Succeeded", "Failed"}


class KubeJobLogStreamer:
    """Stream a build Job's pod logs into the cockpit + rmux sinks (#680).

    Construct with the two sinks (both injectable for tests), an optional
    ``line_source`` (defaults to the real k8s follow-logs stream) and an
    optional ``job_active`` liveness check. Call ``stream`` to pump until the
    Job stops running; it never raises.

    #1619: without ``job_active`` the pump makes one pass and returns on the
    first end-of-stream — which, against a pod whose app container had not
    started yet, meant the whole build was never followed.
    """

    def __init__(
        self,
        *,
        log_sink: LogLineSink,
        rmux_feed: RmuxFeed | None = None,
        line_source: LineSource | None = None,
        plan_sync: PlanSync | None = None,
        plan_sync_interval: float = _PLAN_SYNC_INTERVAL_SECONDS,
    ) -> None:
        self._log_sink = log_sink
        self._rmux_feed = rmux_feed
        self._line_source = line_source or _default_line_source
        self._plan_sync = plan_sync
        self._plan_sync_interval = plan_sync_interval
        # ``None`` rather than 0.0, and the difference is not cosmetic: the
        # clock below is ``time.monotonic()``, i.e. time since BOOT. A 0.0 seed
        # reads as "already synced, at boot", so a Job pod scheduled within one
        # interval of a node coming up would skip its first pull and show a
        # fully-pending DAG for that whole interval. None means "never synced".
        self._plan_synced_at: float | None = None

    async def _should_reattach(
        self, empty_reattaches: int, namespace: str, job_name: str, delivered: int
    ) -> bool:
        """Whether a clean end-of-stream means "go round again" (#1619).

        False in three cases: no liveness authority was injected (one pass, the
        pre-#1619 behaviour, which keeps the streamer usable and its tests
        honest); the Job is done, so this EOF really was the end; or an active
        Job has produced nothing across enough reattaches that following it is
        pointless — which is said out loud rather than silently abandoned.
        """
        if self._job_active is None:
            return False
        if not await self._job_active():
            return False
        if empty_reattaches >= _MAX_EMPTY_REATTACHES:
            _log.warning(
                "[build_log_stream] Job %s/%s reports active but produced "
                "nothing across %d reattaches — giving up on live logs "
                "(build unaffected, %d line(s) delivered)",
                namespace,
                job_name,
                empty_reattaches,
                delivered,
            )
            return False
        return True

    async def stream(
        self,
        *,
        namespace: str,
        job_name: str,
        spec_id: str,
        job_active: JobActive | None = None,
    ) -> int:
        """Pump the Job pod's logs into both sinks. Returns lines streamed.

        Best-effort: a failing line-source, a sink that raises, or a mid-stream
        drop is logged and ends the pump cleanly — the build and the reconcile
        loop are entirely independent of this. Returns the count of lines
        delivered (for observability + tests).
        """
        delivered = 0
        self._job_active = job_active
        try:
            # #1619: a clean end-of-stream is NOT proof the build ended. The
            # only authority on that is the reconcile loop, which cancels this
            # task when the Job goes terminal. So an EOF means "reattach", and
            # the loop below exits by cancellation, by the pod never starting,
            # or by giving up loudly after too many empty reattaches.
            empty_reattaches = 0
            # Lines READ from the source, which is not the same as lines
            # delivered: an empty raw line is consumed but never fanned out,
            # and ``delivered`` is this method's return contract. Conflating
            # the two inflates the count that callers and tests rely on.
            consumed = 0
            while True:
                seen = 0
                async for raw in self._line_source(namespace, job_name):
                    seen += 1
                    # Re-following returns the pod log from the beginning, so
                    # skip what was already read rather than replaying it.
                    # Chosen over the API's ``since_time`` because it needs no
                    # change to the injectable LineSource signature and cannot
                    # drop a line at a second-granularity seam (see the plan's
                    # recorded deviation).
                    if seen <= consumed:
                        continue
                    consumed = seen
                    if not raw:
                        continue
                    await self._fan_out(spec_id, raw)
                    delivered += 1
                empty_reattaches = 0 if seen > 0 else empty_reattaches + 1
                if not await self._should_reattach(
                    empty_reattaches, namespace, job_name, delivered
                ):
                    break
                await asyncio.sleep(_REATTACH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # Reconcile/stop cancelled us — the Job reached a terminal state or
            # the build was deleted. Propagate so the task is properly cancelled.
            raise
        except Exception:  # noqa: BLE001 - log streaming is never load-bearing
            _log.warning(
                "[build_log_stream] log stream for Job %s/%s ended on error "
                "after %d line(s) (build unaffected)",
                namespace,
                job_name,
                delivered,
                exc_info=True,
            )
        else:
            still_active = False
            if self._job_active is not None:
                with contextlib.suppress(Exception):
                    still_active = await self._job_active()
            if still_active:
                # #1619: this is the shape that hid the defect — "completed
                # (1 line(s))" at INFO, against a build that then ran for 145
                # minutes. A stream that stops while its Job is alive is a
                # fault, and now says so.
                _log.warning(
                    "[build_log_stream] Job %s/%s log stream ended after "
                    "%d line(s) while the Job is still active",
                    namespace,
                    job_name,
                    delivered,
                )
            else:
                _log.info(
                    "[build_log_stream] Job %s/%s log stream completed (%d line(s))",
                    namespace,
                    job_name,
                    delivered,
                )
        return delivered

    async def _fan_out(self, spec_id: str, raw: bytes) -> None:
        """Deliver one raw log line to both sinks, mirroring the in-pod path.

        rmux gets raw bytes with ``\\n`` → ``\\r\\n`` (xterm needs CRLF, exactly
        as ``AgentService._process_output``); the cockpit gets the decoded,
        right-stripped line. Each sink is isolated so one raising never starves
        the other.
        """
        if self._rmux_feed is not None:
            try:
                self._rmux_feed(spec_id, raw.replace(b"\n", b"\r\n"))
            except Exception:  # noqa: BLE001 - rmux mirror is best-effort
                _log.debug(
                    "[build_log_stream] rmux feed raised (ignored)", exc_info=True
                )
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            return
        try:
            await self._log_sink(line)
        except Exception:  # noqa: BLE001 - cockpit sink is best-effort
            _log.debug(
                "[build_log_stream] cockpit log sink raised (ignored)", exc_info=True
            )
        await self._maybe_sync_plan()

    async def _maybe_sync_plan(self) -> None:
        """Pull the Job's advanced plan onto the control plane, throttled (#1228).

        The third sink, and the only one that is not observability: per-subtask
        ``status``/``started_at`` live in ``implementation_plan.json``, which on
        the packed path is written inside the Job's ephemeral ``/work`` and
        reached the control plane only at completion. CFactory's live execution
        DAG reads those fields, so a running build rendered every node
        ``waiting`` for its whole duration and flipped all of them to done in one
        step at the end.

        Nothing new is pushed or parsed: the Job already publishes the plan to
        object storage and the control plane already pulls it — both just once,
        at the end. This calls the existing pull on a timer while the build runs.

        Uses ``monotonic`` so an NTP step cannot park the throttle in the future
        and freeze the DAG for the rest of the build. Best-effort like the other
        sinks: a store that is unreachable costs a live DAG, never the build.
        """
        if self._plan_sync is None:
            return
        now = asyncio.get_running_loop().time()
        if (
            self._plan_synced_at is not None
            and now - self._plan_synced_at < self._plan_sync_interval
        ):
            return
        # Stamp BEFORE awaiting, not after: _fan_out is re-entered while this
        # sync is in flight, and a stamp afterwards would let every line racing
        # the first sync start its own.
        self._plan_synced_at = now
        try:
            await asyncio.to_thread(self._plan_sync)
        except Exception:  # noqa: BLE001 - plan sync is best-effort
            _log.debug("[build_log_stream] plan sync raised (ignored)", exc_info=True)
