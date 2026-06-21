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
"""

from __future__ import annotations

import asyncio
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
                namespace, job_name,
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
                namespace, job_name, exc_info=True,
            )
            pods = None
        items = getattr(pods, "items", None) or []
        if items:
            name = items[0].metadata.name
            if name:
                return str(name)
        await asyncio.sleep(_POD_WAIT_INTERVAL_SECONDS)
    return None


class KubeJobLogStreamer:
    """Stream a build Job's pod logs into the cockpit + rmux sinks (#680).

    Construct with the two sinks (both injectable for tests) and an optional
    ``line_source`` (defaults to the real k8s follow-logs stream). Call
    ``stream`` to pump until the Job pod's log stream ends; it never raises.
    """

    def __init__(
        self,
        *,
        log_sink: LogLineSink,
        rmux_feed: RmuxFeed | None = None,
        line_source: LineSource | None = None,
    ) -> None:
        self._log_sink = log_sink
        self._rmux_feed = rmux_feed
        self._line_source = line_source or _default_line_source

    async def stream(self, *, namespace: str, job_name: str, spec_id: str) -> int:
        """Pump the Job pod's logs into both sinks. Returns lines streamed.

        Best-effort: a failing line-source, a sink that raises, or a mid-stream
        drop is logged and ends the pump cleanly — the build and the reconcile
        loop are entirely independent of this. Returns the count of lines
        delivered (for observability + tests).
        """
        delivered = 0
        try:
            async for raw in self._line_source(namespace, job_name):
                if not raw:
                    continue
                await self._fan_out(spec_id, raw)
                delivered += 1
        except asyncio.CancelledError:
            # Reconcile/stop cancelled us — the Job reached a terminal state or
            # the build was deleted. Propagate so the task is properly cancelled.
            raise
        except Exception:  # noqa: BLE001 - log streaming is never load-bearing
            _log.warning(
                "[build_log_stream] log stream for Job %s/%s ended on error "
                "after %d line(s) (build unaffected)",
                namespace, job_name, delivered, exc_info=True,
            )
        else:
            _log.info(
                "[build_log_stream] Job %s/%s log stream completed (%d line(s))",
                namespace, job_name, delivered,
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
