"""S3-compatible workspace storage (Epic #35 #40 half-B PR-1).

Wraps the local-disk project workspace with optional snapshot-to-S3
durability. Agents never speak to S3 directly — they always read +
write the local POSIX path under ``workspace_root()``. This module
operates one level outside, snapshotting the project dir to S3 at
task phase boundaries and lazily restoring it when a cold pod first
accesses a project whose local path is missing.

## Optional remote

When ``settings.WORKSPACE_S3_URI_BASE`` is empty, the store is a
no-op: ``is_remote()`` returns False, ``upload_project`` /
``download_project`` short-circuit, all behavior matches v1.0 PVC-only
exactly. Setting a non-empty fsspec URI (``s3://``, ``gs://``,
``azure://``) enables the snapshot path.

## Per-project keying

Snapshots are keyed by ``{base}/{org_id}/{project_id}/``. The whole
project workspace (cloned repo + nested ``.aifactory/specs/*/`` +
``.aifactory/worktrees/tasks/*/``) goes in one snapshot. Triggers
fire per-task (every phase transition on every task) but each
trigger uploads the whole project — see the design doc for the
rationale.

## Failure-safe

Upload + download both catch all exceptions internally and log a
WARNING. A Redis-style outage or expired credential cannot crash
the calling code path (a task continuing to execute, an HTTP
request returning a project). Matches the ``audit_service``
pattern.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings

logger = logging.getLogger(__name__)

# Manifest wire-format version. Bump when adding fields old readers
# shouldn't silently ignore. Older readers log a warning + treat the
# snapshot as missing on mismatch (forces a fresh clone — safer than
# guessing).
MANIFEST_VERSION = 1
MANIFEST_FILENAME = "_manifest.json"


class WorkspaceStore:
    """fsspec-backed project workspace snapshotting.

    Instantiate via ``WorkspaceStore.from_settings()`` — that handles
    the empty-string-means-local fallback uniformly across callers.
    """

    def __init__(self, base_uri: str) -> None:
        self._base_uri = base_uri.rstrip("/")

    @classmethod
    def from_settings(cls) -> WorkspaceStore:
        """Build a store from the current ``WORKSPACE_S3_URI_BASE``
        setting. Empty string → local-only no-op store."""
        return cls(get_settings().WORKSPACE_S3_URI_BASE or "")

    def is_remote(self) -> bool:
        """True when the storage layer is engaged.

        Returns False only for an empty base URI (laptop / dev /
        unset default). Any non-empty URI — including explicit
        ``file://`` — counts as "engaged": the store will call
        through to fsspec. ``file://`` is a real operator choice
        for cases like 'run with the snapshot layer enabled but
        backed by a different local disk for testing'.
        """
        return bool(self._base_uri)

    def _key_for_project(self, *, org_id: str, project_id: str) -> str:
        """Compose the per-project key prefix."""
        return f"{self._base_uri}/{org_id}/{project_id}"

    # -------------------------------------------------------------------
    # Upload
    # -------------------------------------------------------------------

    async def upload_project(
        self,
        *,
        org_id: str,
        project_id: str,
        local_path: Path,
        triggered_by_task_id: str | None = None,
        triggered_by_phase: str | None = None,
    ) -> None:
        """Snapshot the project workspace to S3.

        Writes ``_manifest.json`` LAST so a concurrent reader either
        sees a complete snapshot (manifest present + matches) or a
        treat-as-missing one (manifest absent → caller falls back to
        fresh clone). Failure-safe: never raises into the caller.
        """
        if not self.is_remote():
            return
        if not local_path.exists():
            logger.warning(
                "workspace_store.upload_project: local_path %s does not exist; skipping",
                local_path,
            )
            return

        try:
            fs, prefix = self._open_fs()
        except Exception:
            logger.warning(
                "workspace_store.upload_project: failed to open fsspec filesystem",
                exc_info=True,
            )
            return

        key_root = f"{prefix}/{org_id}/{project_id}"

        try:
            # Clear any prior partial snapshot first so the manifest
            # absence on the next reader is meaningful.
            try:
                fs.rm(f"{key_root}/{MANIFEST_FILENAME}")
            except Exception:
                pass  # manifest may not exist — normal on first upload

            # Walk + upload every file under local_path. Track executables
            # so download can re-apply the +x bit (S3 has no POSIX modes).
            # ``fs.put_file`` doesn't auto-mkdir parent dirs on
            # LocalFileSystem (S3 doesn't care — keys are flat), so we
            # explicitly makedirs each parent. Cached set avoids
            # re-issuing makedirs for every file in the same dir.
            file_count = 0
            total_bytes = 0
            executables: list[str] = []
            mkdir_cache: set[str] = set()
            for entry in _walk_files(local_path):
                rel = entry.relative_to(local_path)
                # Skip the manifest filename if a previous run left one
                # at the local root — would create a confusing self-reference.
                if str(rel) == MANIFEST_FILENAME:
                    continue
                remote_key = f"{key_root}/{rel.as_posix()}"
                remote_parent = remote_key.rsplit("/", 1)[0]
                if remote_parent not in mkdir_cache:
                    try:
                        fs.makedirs(remote_parent, exist_ok=True)
                    except Exception:
                        pass  # S3 doesn't need this; LocalFS needs it
                    mkdir_cache.add(remote_parent)
                fs.put_file(str(entry), remote_key)
                file_count += 1
                total_bytes += entry.stat().st_size
                if entry.stat().st_mode & 0o100:  # user-executable bit
                    executables.append(rel.as_posix())

            manifest = {
                "v": MANIFEST_VERSION,
                "org_id": org_id,
                "project_id": project_id,
                "triggered_by_task_id": triggered_by_task_id,
                "triggered_by_phase": triggered_by_phase,
                "uploaded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "uploaded_by_replica": _self_replica_id(),
                "file_count": file_count,
                "total_bytes": total_bytes,
                "source_replica_pvc": str(local_path),
                "executables": executables,
            }
            # Write manifest LAST so partial-upload detection works.
            manifest_key = f"{key_root}/{MANIFEST_FILENAME}"
            try:
                fs.makedirs(key_root, exist_ok=True)
            except Exception:
                pass
            with fs.open(manifest_key, "w") as fh:
                fh.write(json.dumps(manifest, indent=2))
            logger.info(
                "workspace_store.upload_project: snapshotted %s (%d files, %d bytes) to %s",
                local_path, file_count, total_bytes, key_root,
            )
        except Exception:
            logger.warning(
                "workspace_store.upload_project: failed for org=%s project=%s",
                org_id, project_id, exc_info=True,
            )

    # -------------------------------------------------------------------
    # Download
    # -------------------------------------------------------------------

    async def download_project(
        self,
        *,
        org_id: str,
        project_id: str,
        local_path: Path,
    ) -> bool:
        """Restore the project workspace from S3 to ``local_path``.

        Returns True on full success, False when:
        - No snapshot exists (no manifest at the expected key)
        - Manifest exists but ``project_id`` mismatches (defensive
          against bucket misconfiguration)
        - Manifest version is unknown
        - Download fails mid-flight (logged + local_path scrubbed
          to avoid leaving a half-restored worktree the agent could
          then operate on)

        The caller (typically the lazy-restore wrapper) treats False
        as "fall back to fresh clone."
        """
        if not self.is_remote():
            return False

        try:
            fs, prefix = self._open_fs()
        except Exception:
            logger.warning(
                "workspace_store.download_project: failed to open fsspec filesystem",
                exc_info=True,
            )
            return False

        key_root = f"{prefix}/{org_id}/{project_id}"
        manifest_key = f"{key_root}/{MANIFEST_FILENAME}"

        # Verify manifest first.
        try:
            if not fs.exists(manifest_key):
                logger.debug(
                    "workspace_store.download_project: no manifest at %s — no snapshot to restore",
                    manifest_key,
                )
                return False
            with fs.open(manifest_key, "r") as fh:
                manifest = json.loads(fh.read())
        except Exception:
            logger.warning(
                "workspace_store.download_project: failed to read manifest %s",
                manifest_key, exc_info=True,
            )
            return False

        # Defensive: reject mismatched / unknown-version manifests.
        if manifest.get("v") != MANIFEST_VERSION:
            logger.warning(
                "workspace_store.download_project: manifest version %r != expected %d at %s",
                manifest.get("v"), MANIFEST_VERSION, manifest_key,
            )
            return False
        if manifest.get("project_id") != project_id:
            logger.warning(
                "workspace_store.download_project: manifest project_id %r != expected %r at %s",
                manifest.get("project_id"), project_id, manifest_key,
            )
            return False

        executables = set(manifest.get("executables") or [])

        # Materialize the local dir. If anything fails mid-flight,
        # scrub it so the agent never sees half-restored state.
        try:
            local_path.mkdir(parents=True, exist_ok=True)
            for remote in _list_files(fs, key_root):
                rel = remote[len(key_root) + 1:]
                if rel == MANIFEST_FILENAME:
                    continue
                dest = local_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                fs.get_file(remote, str(dest))
                if rel in executables:
                    mode = dest.stat().st_mode
                    dest.chmod(mode | 0o111)  # u+x, g+x, o+x (matches `chmod +x`)
            logger.info(
                "workspace_store.download_project: restored %s from %s",
                local_path, key_root,
            )
            return True
        except Exception:
            logger.warning(
                "workspace_store.download_project: failed mid-restore for org=%s project=%s — "
                "scrubbing partial local dir to avoid half-restored worktree",
                org_id, project_id, exc_info=True,
            )
            try:
                shutil.rmtree(local_path, ignore_errors=True)
            except Exception:
                pass
            return False

    # -------------------------------------------------------------------
    # Existence check
    # -------------------------------------------------------------------

    async def project_exists(
        self, *, org_id: str, project_id: str,
    ) -> bool:
        """Cheap existence check: does a complete snapshot exist?

        Used by the lazy-restore path on cold-pod load_projects() flows
        to skip the download attempt when no snapshot is present.
        """
        if not self.is_remote():
            return False
        try:
            fs, prefix = self._open_fs()
            return bool(fs.exists(f"{prefix}/{org_id}/{project_id}/{MANIFEST_FILENAME}"))
        except Exception:
            logger.warning(
                "workspace_store.project_exists: probe failed for org=%s project=%s",
                org_id, project_id, exc_info=True,
            )
            return False

    # -------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------

    def _open_fs(self) -> tuple[object, str]:
        """Return ``(fsspec_filesystem, key_prefix)`` for the configured
        base URI. fsspec is imported lazily so the no-op path doesn't
        pay for the import."""
        import fsspec

        # fsspec.url_to_fs returns (filesystem, path_within_filesystem).
        # Pass any S3/MinIO endpoint via env (the chart sets
        # FSSPEC_S3_ENDPOINT_URL and FSSPEC_S3_ADDRESSING_STYLE).
        client_kwargs: dict = {}
        endpoint = os.environ.get("FSSPEC_S3_ENDPOINT_URL")
        if endpoint:
            client_kwargs["endpoint_url"] = endpoint
        addressing = os.environ.get("FSSPEC_S3_ADDRESSING_STYLE")
        config_kwargs: dict = {}
        if addressing and addressing.lower() in ("path", "virtual"):
            config_kwargs["s3"] = {"addressing_style": addressing.lower()}

        fs, path = fsspec.url_to_fs(
            self._base_uri,
            client_kwargs=client_kwargs or None,
            config_kwargs=config_kwargs or None,
        )
        return fs, path.rstrip("/")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _walk_files(root: Path):
    """Yield every regular file under ``root`` (recursive)."""
    for entry in root.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            yield entry


def _list_files(fs, key_root: str) -> list[str]:
    """List every file under ``key_root`` on ``fs``. Returns absolute
    keys (i.e. including ``key_root`` prefix)."""
    # fs.find returns posix-style paths. Strip trailing slashes so
    # comparisons are predictable.
    return [p.rstrip("/") for p in fs.find(key_root) if not p.endswith("/")]


def _self_replica_id() -> str:
    """Pull the replica UUID from the event bus when available, so
    audit traces correlate. Falls back to a fresh UUID for stand-
    alone use (e.g. CLI / test contexts that don't start the bus)."""
    try:
        from ..websockets.event_bus import self_replica_id
        return self_replica_id
    except Exception:
        import uuid
        return str(uuid.uuid4())
