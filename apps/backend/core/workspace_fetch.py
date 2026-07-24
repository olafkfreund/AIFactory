"""RFC-0017 Stage E (#190) consumer — materialize ``/work`` from object storage.

The build_backend producer (``apps/web-server``) packs the populated build
worktree to object storage and sets the Job's ``WORKSPACE_URI`` env when
``AIFACTORY_PACK_WORKSPACE`` is on. On the Job side, run.py must reconstitute
``/work`` from that archive BEFORE it resolves the spec and builds — replacing the
single-node RWO co-mount that pins a Job to one node (the multi-node scale-out
path, ``apis/concurrency-conventions.md`` §2.3).

Inert by default: ``WORKSPACE_URI`` is only set on a packed-workspace Job, so on
the co-mount path (and every non-Job invocation) ``maybe_unpack_workspace`` is a
no-op and today's behaviour is unchanged.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

WORKSPACE_URI_ENV = "WORKSPACE_URI"


def _mark_git_safe_directory(dest: Path) -> None:
    """Tell git the unpacked repo is trusted (RFC-0017 #190).

    The packed workspace is unpacked into ``/work``, whose emptyDir mount root is
    created ``root``-owned by the kubelet while the build runs as nonroot. git
    then refuses every repo op with ``detected dubious ownership in repository at
    '/work'`` (caught in live #190 validation, downstream of a *successful*
    unpack). The single-node co-mount path never hit this — its PVC subPath was
    created by the nonroot control plane, so ownership already matched. Marking
    the dir safe is correct for a single-shot, single-tenant build container.

    Best-effort: a failure here is logged, not raised — the unpack already
    succeeded, and any genuine git problem still surfaces in the build itself.
    """
    # S603/S607 suppressed below: fixed `git` argv (no shell, no untrusted input);
    # `git` by name matches every other git call site in core/ (worktree.py) and
    # the build image puts it on PATH.
    for path in {str(dest), str(dest.resolve())}:
        try:
            subprocess.run(  # noqa: S603
                ["git", "config", "--global", "--add", "safe.directory", path],  # noqa: S607
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            _log.warning("[workspace_fetch] could not mark %s git-safe: %s", path, exc)


def maybe_unpack_workspace(project_dir: str | os.PathLike[str]) -> bool:
    """Materialize ``project_dir`` (``/work``) from the packed ``WORKSPACE_URI``.

    Returns ``True`` when an unpack happened, ``False`` (no-op) when
    ``WORKSPACE_URI`` is absent — today's single-node co-mount path is unchanged.

    Fail-LOUD: unlike the producer (which can fall back to the co-mount), once a
    Job is on the URI path it has no other source for ``/work``, so a fetch /
    extract error propagates rather than letting the build run on an empty tree.
    The extract itself is path-traversal-guarded by ``unpack_workspace`` —
    a poisoned archive is refused before anything is written.
    """
    uri = os.environ.get(WORKSPACE_URI_ENV, "").strip()
    if not uri:
        return False
    # Deferred import — keep boto3 off the import path of every non-Job run; core.*
    # is on sys.path at startup (the cli entrypoint adds the backend dir).
    from core.artifact_store import ArtifactStore, unpack_workspace  # noqa: PLC0415

    dest = Path(project_dir)
    _log.info(
        "[workspace_fetch] unpacking packed workspace %s -> %s (RFC-0017 #190)",
        uri,
        dest,
    )
    unpack_workspace(ArtifactStore(), uri, dest)
    # The unpacked repo lives on a root-owned emptyDir mount but the build runs
    # nonroot → git "dubious ownership". Mark it trusted before run.py touches git.
    _mark_git_safe_directory(dest)
    return True


def maybe_push_workspace_branch(
    project_dir: str | os.PathLike[str], spec_id: str
) -> bool:
    """Push the built worktree branch to origin on the packed path (RFC-0017 #190).

    Symmetric to :func:`maybe_unpack_workspace`. On the packed (multi-node) path the
    build worktree lives on the Job's ephemeral ``/work`` emptyDir, which is GONE
    once the Job exits — so the control-plane handoff/PR-endgame push
    (``tfactory_client._git_info_and_push`` / ``create-pr``), which reads the
    control-plane data-PVC worktree, finds nothing and degrades to ``main`` (the
    produced code is lost). Pushing the branch HERE, from inside the Job where
    ``/work`` still holds the self-contained clone (origin + ``GITHUB_TOKEN``),
    persists the build output to GitHub before the Job dies.

    No-op (returns ``False``) when ``WORKSPACE_URI`` is absent — the co-mount path
    keeps ``/work`` on the data PVC, so the existing control-plane push is unchanged.
    Best-effort: never raises (a push failure must not fail an otherwise-green build).
    """
    if not os.environ.get(WORKSPACE_URI_ENV, "").strip():
        return False
    wt = Path(project_dir) / ".aifactory" / "worktrees" / "tasks" / spec_id
    if not wt.is_dir():
        _log.warning("[workspace_fetch] no worktree at %s; nothing to push", wt)
        return False

    def _git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        ).stdout.strip()

    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        url = _git("remote", "get-url", "origin")
        if not branch or not url:
            _log.warning("[workspace_fetch] worktree has no branch/origin; skip push")
            return False
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        push_url = url
        if token and url.startswith("https://github.com/"):
            push_url = url.replace("https://", f"https://x-access-token:{token}@", 1)
        res = subprocess.run(  # noqa: S603
            ["git", "push", push_url, f"HEAD:{branch}"],  # noqa: S607
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if res.returncode != 0:
            _log.warning("[workspace_fetch] branch push failed: %s", res.stderr[:200])
            return False
        _log.info(
            "[workspace_fetch] pushed build branch %s to origin (packed path)", branch
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:  # never break a green build
        _log.warning("[workspace_fetch] branch push errored: %s", exc)
        return False


_USAGE_FILE = "token_usage.json"


def _usage_key(spec_id: str) -> str:
    """Deterministic object key for a task's token_usage.json (RFC-0017 #190).

    Both the Job (producer) and the control plane (consumer) derive the SAME key
    from ``spec_id`` alone — no URI threading needed.
    """
    from core.artifact_store import ArtifactRef  # noqa: PLC0415

    return str(
        ArtifactRef(
            service="aifactory", job_id=spec_id, role="build", path=_USAGE_FILE
        ).key()
    )


def maybe_push_usage(spec_dir: str | os.PathLike[str], spec_id: str) -> bool:
    """Push the Job's ``token_usage.json`` to object storage on the packed path.

    Same propagation gap as :func:`maybe_push_workspace_branch`: the build writes
    ``token_usage.json`` into the Job's ephemeral ``/work`` spec dir, but the
    completion-event emitter runs on the CONTROL PLANE and reads the data-PVC spec
    dir — never populated on the packed path — so CFactory shows zero token usage.
    Uploading it here (keyed by ``spec_id``) lets the control plane fetch it.

    No-op off the packed path (``WORKSPACE_URI`` unset) where the spec dir is on
    the data PVC already. Best-effort: never raises.
    """
    if not os.environ.get(WORKSPACE_URI_ENV, "").strip():
        return False
    src = Path(spec_dir) / _USAGE_FILE
    if not src.is_file():
        _log.warning("[workspace_fetch] no %s to push", _USAGE_FILE)
        return False
    try:
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        ArtifactStore().put_bytes(
            _usage_key(spec_id), src.read_bytes(), "application/json", role="build"
        )
        _log.info(
            "[workspace_fetch] pushed %s to object store (packed path)", _USAGE_FILE
        )
        return True
    except Exception as exc:  # noqa: BLE001 - must never break a green build
        _log.warning("[workspace_fetch] usage push failed: %s", exc)
        return False


def maybe_fetch_usage(spec_dir: str | os.PathLike[str], spec_id: str) -> bool:
    """Control-plane counterpart to :func:`maybe_push_usage`.

    If the data-PVC spec dir has no ``token_usage.json`` (the packed path never
    wrote it there), fetch the one the Job pushed and write it locally so
    ``completion.read_usage`` finds it and the completion event carries real token
    usage to CFactory. No-op when the file already exists (co-mount path) or no
    pushed copy exists. Best-effort: never raises.
    """
    dest = Path(spec_dir) / _USAGE_FILE
    if dest.is_file():
        return False
    try:
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        data = ArtifactStore().get_bytes(_usage_key(spec_id))
    except Exception as exc:  # noqa: BLE001 - no pushed usage / store unreachable
        _log.debug("[workspace_fetch] no pushed usage to fetch: %s", exc)
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        _log.info(
            "[workspace_fetch] fetched %s from object store (packed path)", _USAGE_FILE
        )
        return True
    except OSError as exc:
        _log.warning("[workspace_fetch] could not write fetched usage: %s", exc)
        return False


_TASK_LOGS_FILE = "task_logs.json"


def _task_logs_key(spec_id: str) -> str:
    """Deterministic object key for a task's task_logs.json (W1, Factory #218).

    Same producer/consumer key derivation as :func:`_usage_key`.
    """
    from core.artifact_store import ArtifactRef  # noqa: PLC0415

    return str(
        ArtifactRef(
            service="aifactory", job_id=spec_id, role="build", path=_TASK_LOGS_FILE
        ).key()
    )


def maybe_push_task_logs(spec_dir: str | os.PathLike[str], spec_id: str) -> bool:
    """Push the Job's ``task_logs.json`` to object storage on the packed path.

    ``task_logs.json`` is the authoritative per-phase status file
    (planning/coding/qa → running/done/failed). On the packed path the build
    writes it into the Job's ephemeral ``/work`` spec dir, but ``spec_to_task``
    runs on the CONTROL PLANE and reads the data-PVC spec dir — never populated —
    so the task list defaults to ``backlog`` and CFactory shows ``queued`` forever
    even after a task built/merged/deployed (Factory #218). Uploading it here
    (keyed by ``spec_id``) lets the control plane fetch the real status.

    No-op off the packed path (``WORKSPACE_URI`` unset). Best-effort: never raises.
    """
    if not os.environ.get(WORKSPACE_URI_ENV, "").strip():
        return False
    src = Path(spec_dir) / _TASK_LOGS_FILE
    if not src.is_file():
        _log.warning("[workspace_fetch] no %s to push", _TASK_LOGS_FILE)
        return False
    try:
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        ArtifactStore().put_bytes(
            _task_logs_key(spec_id), src.read_bytes(), "application/json", role="build"
        )
        _log.info(
            "[workspace_fetch] pushed %s to object store (packed path)", _TASK_LOGS_FILE
        )
        return True
    except Exception as exc:  # noqa: BLE001 - must never break a green build
        _log.warning("[workspace_fetch] task_logs push failed: %s", exc)
        return False


def maybe_fetch_task_logs(spec_dir: str | os.PathLike[str], spec_id: str) -> bool:
    """Control-plane counterpart to :func:`maybe_push_task_logs`.

    If the data-PVC spec dir has no ``task_logs.json`` (the packed path never
    wrote it there), fetch the one the Job pushed and write it locally so
    ``spec_to_task`` reports the real per-phase status (running/done/failed)
    instead of falling back to ``backlog``. No-op when the file already exists
    (co-mount path) or no pushed copy exists. Best-effort: never raises.
    """
    dest = Path(spec_dir) / _TASK_LOGS_FILE
    if dest.is_file():
        return False
    try:
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        data = ArtifactStore().get_bytes(_task_logs_key(spec_id))
    except Exception as exc:  # noqa: BLE001 - no pushed logs / store unreachable
        _log.debug("[workspace_fetch] no pushed task_logs to fetch: %s", exc)
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        _log.info(
            "[workspace_fetch] fetched %s from object store (packed path)",
            _TASK_LOGS_FILE,
        )
        return True
    except OSError as exc:
        _log.warning("[workspace_fetch] could not write fetched task_logs: %s", exc)
        return False


_PLAN_FILE = "implementation_plan.json"


def _plan_key(spec_id: str) -> str:
    """Deterministic object key for a task's implementation_plan.json (#852).

    Same derivation as :func:`_usage_key` / :func:`_task_logs_key`: both sides
    compute it from ``spec_id`` alone, so no URI threading is needed.
    """
    from core.artifact_store import ArtifactRef  # noqa: PLC0415

    return str(
        ArtifactRef(
            service="aifactory", job_id=spec_id, role="build", path=_PLAN_FILE
        ).key()
    )


def maybe_push_plan(spec_dir: str | os.PathLike[str], spec_id: str) -> bool:
    """Push the Job's ``implementation_plan.json`` to object storage (#852).

    The third file with the same propagation gap as
    :func:`maybe_push_workspace_branch` / :func:`maybe_push_usage` — and the most
    consequential. The build marks each subtask ``completed`` in the plan inside
    its ephemeral ``/work``, but the control plane counts completed subtasks from
    the data-PVC spec dir, which the packed path never populates. It therefore
    sees 0 completed and applies the #287 guard — "a clean exit with no successful
    subtask is emitted as FAILED" — so EVERY successful packed build escalated to
    human_review + errors, blocking the TFactory handoff behind a bookkeeping gap
    (Factory#253 "output propagation").

    No-op off the packed path (``WORKSPACE_URI`` unset) where the spec dir is on
    the data PVC already. Best-effort: never raises — a push failure must not turn
    a green build red.
    """
    if not os.environ.get(WORKSPACE_URI_ENV, "").strip():
        return False
    src = Path(spec_dir) / _PLAN_FILE
    if not src.is_file():
        _log.warning("[workspace_fetch] no %s to push", _PLAN_FILE)
        return False
    try:
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        ArtifactStore().put_bytes(
            _plan_key(spec_id), src.read_bytes(), "application/json", role="build"
        )
        _log.info(
            "[workspace_fetch] pushed %s to object store (packed path)", _PLAN_FILE
        )
        return True
    except Exception as exc:  # noqa: BLE001 - must never break a green build
        _log.warning("[workspace_fetch] plan push failed: %s", exc)
        return False


def maybe_fetch_plan(spec_dir: str | os.PathLike[str], spec_id: str) -> bool:
    """Control-plane counterpart to :func:`maybe_push_plan` (#852).

    Unlike its siblings this OVERWRITES an existing local file. The others fetch
    artifacts the control plane never has; the plan is different — the control
    plane wrote the ORIGINAL (all subtasks ``pending``) before dispatch, so a
    bail-if-present guard would keep the stale pre-build copy and preserve the
    exact bug this fixes. The Job's copy is authoritative: it is the same file,
    advanced.

    No-op off the packed path or when nothing was pushed. Best-effort: never
    raises.
    """
    try:
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        data = ArtifactStore().get_bytes(_plan_key(spec_id))
    except Exception as exc:  # noqa: BLE001 - no pushed plan / store unreachable
        _log.debug("[workspace_fetch] no pushed plan to fetch: %s", exc)
        return False
    dest = Path(spec_dir) / _PLAN_FILE
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        _log.info(
            "[workspace_fetch] fetched %s from object store (packed path)", _PLAN_FILE
        )
        return True
    except OSError as exc:
        _log.warning("[workspace_fetch] could not write fetched plan: %s", exc)
        return False
