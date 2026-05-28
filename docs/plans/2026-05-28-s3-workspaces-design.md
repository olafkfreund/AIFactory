# Design: S3-compatible workspace storage

> Sub-spec of Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) child [#40](https://github.com/olafkfreund/AIFactory/issues/40) (half-B — the S3 half). The Redis pub/sub half (half-A) shipped in PRs #171 + #172.

## Summary

Make AIFactory workspaces durable beyond pod / PVC lifetime by snapshotting them to S3-compatible storage at task phase boundaries, with lazy restore on first access from any replica. Combined with half-A (Redis WS fan-out), this completes the multi-replica readiness story for Epic #35 — pods can scale, die, and be replaced without losing in-flight task state.

Agents NEVER speak to S3 directly. Their entire view of the workspace stays on a real POSIX filesystem under `workspace_root()`. The new `WorkspaceStore` operates one level outside, syncing the workspace tree to/from S3 at well-defined moments. This dodges the POSIX-semantics minefield that S3-backed FUSE mounts hit (atomic rename + hardlinks + getxattr breakages that turn `git commit` flaky).

Disabled by default: `WORKSPACE_S3_URI_BASE` unset → behavior is byte-for-byte the v1.0 PVC-only model. Laptop installs and single-replica pilots have zero new infra dep.

## Decisions locked

| Decision | Choice | Why |
|---|---|---|
| Storage migration boundary | Snapshot-at-task-boundaries (NOT CSI mount or FUSE bind) | Agents shell out to `git` for clone/commit; `git`'s `.git/index.lock → .git/index` rename pattern + hardlinks in `.git/objects` are documented to fail on every S3-backed FUSE. Snapshot keeps agents on real POSIX. |
| Snapshot trigger semantics | On phase transitions (`coding`, `review_pending`, `completed`, `failed`) + worktree-merge | ~3-4 uploads per task. Phase boundaries are natural checkpoints; state is most coherent between agent runs. Worst-case loss on pod death = the work-in-progress for the current phase. No periodic timers, no continuous sync (would explode S3 PUT cost). |
| Per-tenant key shape | Deploy-time `WORKSPACE_S3_URI_BASE` + per-request `{org_id}/{project_id}/{task_id}` | IAM-bounded by the deploy-time prefix. Forward-compatible with Epic #36 Tenant Isolation (one AIFactory deployment per tenant namespace = one bucket/prefix per tenant naturally). No DB table for `org→bucket` mapping needed. |
| Backend scope | AWS S3 + MinIO first-class; Azure / GCS supported via env-var convention but un-typed-in-chart | AWS + MinIO share the s3fs code path. Azure (adlfs) and GCS (gcsfs) work via fsspec; chart documents the env-var convention without typed values blocks. Honest scope; matches what one PR can prove. |
| Restore semantics | Lazy on-demand on first access | Every caller of the workspace path gets transparent download-if-missing. No eager scan, no explicit `resume_task` API. |
| Concurrency model | Single-writer per task (existing scheduling invariant) | Tasks pin to one replica; agent subprocess lives there. No S3-level locking needed in v1.1. |
| Partial-snapshot protection | `_manifest.json` written LAST | Download fetches manifest first; missing/stale → treat as incomplete, fall back to fresh clone. |
| Agent process auto-resume | Out of scope | Workspaces survive pod death (restorable to new pod); user re-triggers tasks manually to pick up from last phase-boundary snapshot. v1.2 work if it becomes a real need. |
| Retention / cleanup | Out of scope | Operators set S3 bucket-level lifecycle policies. App doesn't manage retention. |
| What gets snapshotted | **Per-project**: entire dir at `{workspace_root}/{project_slug}/` (the cloned repo + all its nested `.aifactory/specs/*/` + `.aifactory/worktrees/tasks/*/` state) | Aligns with how `load_projects()` consumes the path today — the project record's `path` field points at this dir, and route handlers / agent_service read from it directly. One snapshot per project covers all of its tasks; a cold pod that needs project P downloads ONCE and has everything (no re-clone of the parent repo needed). Per-task snapshots were considered but require a second restore path (re-clone parent from git remote) — extra moving parts for no real gain at pilot scale. |

## Architecture

```
agent_service ... worktree-merger ... project_workspace_service.clone_or_update()
       │              │                     │
       ▼              ▼                     ▼
     ── workspace_root() / {task_slug}/  (real POSIX FS — agents always see this) ──
                          ▲
                          │  download-if-missing on first access
                          │  upload on phase transitions
                          ▼
              ┌───────────────────────────────┐
              │   WorkspaceStore              │  [ NEW: services/workspace_store.py ]
              │   (fsspec-backed abstraction) │
              └───────────┬───────────────────┘
                          │
                          ▼
                 protocol://{base}/{org_id}/{project_id}/{task_id}/
                          │
            ┌─────────────┼─────────────┬────────────┐
            ▼             ▼             ▼            ▼
       LocalFS         S3/MinIO    AzureBlob*     GCS*
   (laptop/unset)    (1st class)  (untested)  (untested)
```

**Key property**: agents never speak to S3. The store wraps the existing PVC-or-local workspace path with upload/download hooks. A pod that doesn't have the task's workspace locally gets it transparently restored on first access.

## Data: S3 key shape

```
{WORKSPACE_S3_URI_BASE}/{org_id}/{project_id}/<file...>
{WORKSPACE_S3_URI_BASE}/{org_id}/{project_id}/_manifest.json   ← uploaded LAST
```

Per-project keying (not per-task) — `{task_id}` was originally in the design but the audit confirmed the workspace storage unit is the project dir (the cloned repo + its nested `.aifactory/specs/*/` + worktrees), not individual tasks. Per-task triggers still fire (every phase transition on every task), but each trigger uploads the whole project dir.

`WORKSPACE_S3_URI_BASE` is a single fsspec URI:
- `s3://aifactory-prod/workspaces` — AWS S3
- `s3://my-bucket/workspaces` + `FSSPEC_S3_ENDPOINT_URL=...` — MinIO
- `gs://my-bucket/workspaces` — GCS (env-var auth)
- `azure://container/aifactory` — Azure Blob (env-var auth)
- `file:///var/lib/aifactory/workspaces` — explicit local (rarely needed; empty string achieves the same default)

## Data: manifest format

```json
{
  "v": 1,
  "org_id": "org-99",
  "project_id": "proj-42",
  "triggered_by_task_id": "001-add-auth",
  "triggered_by_phase": "review_pending",
  "uploaded_at": "2026-05-28T11:23:45Z",
  "uploaded_by_replica": "f0e9d8c7-...",
  "file_count": 1843,
  "total_bytes": 234567890,
  "source_replica_pvc": "/var/lib/aifactory/workspaces/my-repo",
  "executables": [
    "scripts/run.sh",
    "tools/migrate.py"
  ]
}
```

- `v` — version field (matches the Redis envelope convention from #171). v1 readers tolerate unknown future fields; future readers reject v1 with a warning.
- `uploaded_by_replica` — pulled from `event_bus.self_replica_id` for cross-referencing with the audit log + WS traffic.
- `executables` — relative paths of files where the user-executable bit was set at upload time. S3 itself has no POSIX mode bits, so this list is how `download` restores `+x`. For git-tracked files `git checkout` would re-apply modes from the index, but loose untracked files (e.g. drill scripts in `.aifactory/specs/X/`) would lose `+x` without this. Files with no mode bits beyond default `0644` are omitted to keep the manifest small.
- Manifest is the LAST file written on every upload. Download verifies manifest exists + `task_id` matches expected; absent or mismatched → treat as incomplete.

## Module surface — new `services/workspace_store.py`

```python
class WorkspaceStore:
    def __init__(self, base_uri: str) -> None: ...

    @classmethod
    def from_settings(cls) -> "WorkspaceStore":
        """Read settings.WORKSPACE_S3_URI_BASE. Empty = local-only mode."""

    def is_remote(self) -> bool:
        """True for s3://, gs://, azure://. False for laptop / unset / file://.
        Callers short-circuit upload/download when False."""

    async def upload_project(
        self, *,
        org_id: str, project_id: str,
        local_path: Path,
        triggered_by_task_id: str | None = None,
        triggered_by_phase: str | None = None,
    ) -> None:
        """Snapshot `local_path` (the project workspace root) to
        {base}/{org_id}/{project_id}/. Writes _manifest.json LAST.
        Idempotent: re-running overwrites the prior snapshot.
        Failure-safe: logs WARNING + returns; never raises.

        ``triggered_by_*`` are stored in the manifest only — they're
        useful for audit ("which task's phase transition caused this
        upload?") but don't change the storage location."""

    async def download_project(
        self, *,
        org_id: str, project_id: str,
        local_path: Path,
    ) -> bool:
        """Restore the project workspace to `local_path`. Returns True
        on success, False when no snapshot exists (caller falls back
        to fresh git clone). Verifies _manifest.json first; rejects
        partial snapshots and cleans up the partial local dir before
        returning False."""

    async def project_exists(
        self, *, org_id: str, project_id: str,
    ) -> bool:
        """HEAD on _manifest.json — cheap existence check used by the
        lazy-restore path on cold-pod load_projects() flows."""
```

## Settings (`config.py`)

```python
# Empty = local-only mode (no S3 snapshots). Non-empty value enables
# upload at task phase boundaries + lazy restore on cross-replica access.
# Matches fsspec URI syntax: s3://, gs://, azure://, or file:// for
# explicit local. Default = "" preserves v1.0 behavior exactly.
WORKSPACE_S3_URI_BASE: str = ""
```

## Integration hooks

**Upload — `agent_service._safe_emit_task_status`:**

```python
async def _safe_emit_task_status(self, task_id, status, review_reason=None):
    await emit_task_status(task_id, status, review_reason)
    # NEW — fire snapshot at phase boundaries when store is configured
    if status in ("coding", "review_pending", "completed", "failed"):
        store = WorkspaceStore.from_settings()
        if store.is_remote():
            # Resolve org_id + project_id + the project's local path
            # from `load_projects()` — every task has a known parent
            # project, and the project record carries both `id` and
            # `path` (the local workspace root).
            ctx = await self._resolve_project_context_for_task(task_id)
            await store.upload_project(
                org_id=ctx.org_id,
                project_id=ctx.project_id,
                local_path=ctx.project_path,
                triggered_by_task_id=task_id,
                triggered_by_phase=status,
            )  # upload is failure-safe internally; doesn't crash the caller
```

Snapshot failures are **non-fatal** — the task continues running, the next phase transition retries the upload. Matches the `audit_service.log_audit_event_bg` failure-safe pattern.

**Download — lazy restore at `load_projects()` consumption time:**

The natural restore point is `load_projects()` in `routes/projects.py` — every consumer reads project paths through this accessor. Wrap it (or add a small helper used by the consumers) to lazily restore on cold-pod access:

```python
async def ensure_project_workspace_present(project: dict) -> dict:
    """Return the project record, lazily restoring its workspace from
    S3 if the local path doesn't exist. Idempotent + cheap when the
    workspace is already present (no S3 calls)."""
    local = Path(project["path"])
    if local.exists():
        return project  # hot path — no S3 call
    store = WorkspaceStore.from_settings()
    if store.is_remote():
        org_id = project.get("org_id")
        project_id = project.get("id")
        if org_id and project_id:
            await store.download_project(
                org_id=org_id, project_id=project_id, local_path=local,
            )
    # If restore failed OR no S3 config OR no org_id: fall through and
    # let the caller hit the missing-path branch (it'll surface a
    # user-facing error or fall back to fresh clone).
    return project
```

Callers that today do `proj = load_projects()[pid]; Path(proj["path"])` get migrated to `proj = await ensure_project_workspace_present(load_projects()[pid])`. Per the audit there are ~5 call sites that consume the path (`routes/terminal.py`, `routes/execution.py`, `services/auto_fix_service.py`, and a couple route handlers in `projects.py`). All are already in `async def` handlers, so the await migration is purely mechanical.

**Sync-callers note:** All identified call sites are async. The audit confirmed no sync consumers of the project path. If a sync site appears in implementation, decide between async-promotion or a sync wrapper using `asyncio.run_coroutine_threadsafe`.

## Helm chart additions

Extend the existing `workspaces:` block (don't fork a new top-level key — keeps related workspace config in one place):

```yaml
workspaces:
  # Existing PVC config (unchanged from #82 PR-B)
  enabled: false
  size: 100Gi
  storageClass: ""
  mountPath: /var/lib/aifactory/workspaces

  # NEW (Epic #35 #40 half-B): S3-compatible durable storage.
  # Independent of workspaces.enabled — operators can run with S3
  # only, PVC only, or both (PVC as hot cache + S3 as durable store).
  storage:
    enabled: false
    # fsspec URI base. Required when storage.enabled=true.
    uriBase: ""
    aws:
      # IRSA / Workload Identity — leave Secret unset, set SA annotation
      # outside the chart. Recommended for production.
      useInstanceRole: false
      # Static-creds Secret with keys AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY.
      # Required when useInstanceRole=false AND uriBase is s3://...
      credentialsSecretName: ""
      # MinIO / S3-compatible: override endpoint.
      endpointUrl: ""
      # MinIO often needs path-style; AWS default is virtual.
      addressingStyle: "auto"  # auto | virtual | path
    # Azure / GCS: env-var convention in v1.1 (no typed chart block).
    # Operators set AZURE_STORAGE_CONNECTION_STRING or
    # GOOGLE_APPLICATION_CREDENTIALS via existing extraEnv. Documented
    # in docs/docs/concepts/workspace-storage.md.
```

**Render-time validator** (matches `redis` and `mcpCredentials` patterns):

- `storage.enabled=true` + `uriBase` empty → helm template fails: `"workspaces.storage.enabled=true requires storage.uriBase (e.g. s3://my-bucket/workspaces)"`.
- `storage.enabled=true` + `uriBase` starts `s3://` + `aws.useInstanceRole=false` + `aws.credentialsSecretName` empty → helm template fails: `"workspaces.storage uriBase=s3:// requires either aws.useInstanceRole=true or aws.credentialsSecretName"`.

**Env injection on the deployment** (when `storage.enabled=true`):

```yaml
- name: WORKSPACE_S3_URI_BASE
  value: {{ .Values.workspaces.storage.uriBase | quote }}

# When endpointUrl set (MinIO)
- name: FSSPEC_S3_ENDPOINT_URL
  value: {{ .Values.workspaces.storage.aws.endpointUrl | quote }}
- name: FSSPEC_S3_ADDRESSING_STYLE
  value: {{ .Values.workspaces.storage.aws.addressingStyle | quote }}

# When credentialsSecretName set
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.workspaces.storage.aws.credentialsSecretName }}
      key: AWS_ACCESS_KEY_ID
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.workspaces.storage.aws.credentialsSecretName }}
      key: AWS_SECRET_ACCESS_KEY
```

## Error handling

| Failure | Behavior |
|---|---|
| `WORKSPACE_S3_URI_BASE` set but bucket unreachable at upload time | Log WARNING with task_id + bucket; task continues; next phase-transition upload retries. No exception raised into the caller. |
| Upload fails mid-flight | Log + continue. Manifest is not written, so subsequent download sees absence and falls back to fresh clone. |
| Download fails (network, credential, bucket missing) | Log WARNING; return False from `download()`; caller falls through to fresh-clone path. User-visible effect: task takes longer to start. |
| Download succeeds but manifest is missing/stale | Log WARNING ("partial snapshot detected, ignoring"); delete the partial local directory; return False. Defends against the agent getting a half-restored worktree. |
| `WORKSPACE_S3_URI_BASE` malformed (unknown scheme) | App starts; logs ERROR at startup; `store.is_remote()` returns False so the system runs in degraded local-only mode. Operator sees the error in boot logs. |
| Helm install with `storage.enabled=true` but no `uriBase` | helm template fails with the required-validator message naming the missing field. |

## Testing

**Unit — `tests/test_workspace_store.py`** (uses fsspec's `LocalFileSystem`, no network):

- `upload` writes all files under `local_path` + writes `_manifest.json` last
- `download` to a fresh dir restores file content + perms (mode bits preserved)
- `exists` returns True after a successful upload, False before
- `download` with missing manifest → returns False, cleans up partial files
- `download` with manifest whose `task_id` mismatches expected → returns False (defensive against bucket misconfiguration)
- `from_settings` reads `WORKSPACE_S3_URI_BASE`; empty → `is_remote() == False`
- `is_remote()` returns False for `file://`, True for `s3://` / `gs://` / `azure://`
- Round-trip with deeply nested `.git/objects/pack/*.pack` files — sanity check that fsspec handles binary files identically to direct copy
- **Mode-bit preservation**: tracked files restored with their original mode bits via the manifest (manifest records `mode` per executable file). Loose untracked files restored with `0644` — `git checkout` re-applies modes for tracked files on the next agent invocation, which covers the common case. Documented as a known limitation in the concept doc.
- Concurrent overwrite from same task is non-destructive (last-write-wins; `_manifest.json` reflects whoever wrote last). **Defensive guard only** — single-writer-per-task is the documented invariant (see Decisions locked); this test ensures bucket state stays consistent if a scheduler bug ever violates the invariant.
- `agent_service._safe_emit_task_status` fires upload on `coding` / `review_pending` / `completed` / `failed` and NOT on `planning`
- `agent_service._safe_emit_task_status` doesn't crash when upload raises (failure-safe)

**Integration — `tests/test_workspace_store_integration.py`** (skip-when-unavailable, mirrors the `TEST_REDIS_URL` pattern):

- `TEST_S3_URI_BASE` env (default `s3://aifactory-test/workspaces` against a local MinIO at `http://localhost:9000`)
- Helper checks MinIO reachability + skips if unavailable
- Round-trip: upload a sample workspace tree → exists() returns True → download to a fresh dir → file tree matches byte-for-byte
- Partial-upload simulation: write some files via raw fsspec without manifest → `download` returns False, doesn't pollute local dir
- `WORKSPACE_S3_URI_BASE` switched between two valid bases mid-test → previous uploads at the old base remain accessible (regression guard for prefix-change scenarios)

**Helm — `tests/helm/test_workspace_storage_toggle.py`** (matches `redis_toggle` shape):

- Off → no `WORKSPACE_S3_URI_BASE` / `AWS_*` env on the container
- On + `uriBase=s3://...` + `aws.credentialsSecretName=foo` → env vars land via valueFrom.secretKeyRef
- On + `uriBase=s3://...` + `aws.useInstanceRole=true` → no Secret refs (relies on IRSA), uriBase env still injected
- On + MinIO config (endpointUrl + addressingStyle=path) → both extra envs render
- Validator: `storage.enabled=true` without uriBase → helm template fails with the expected message
- Validator: `storage.enabled=true` + `s3://` uriBase + neither `useInstanceRole` nor `credentialsSecretName` → fails

**CI matrix:**

- Add a MinIO service container to the existing `backend (ruff + pytest)` GHA job alongside the Redis one (Redis was added in #172). Set `TEST_S3_URI_BASE` env.
- Helm tests run in the existing helm-acceptance job — no new infra.

## Migration

No data migration. Code path is additive: existing in-process behavior preserved when `WORKSPACE_S3_URI_BASE` is unset. Rolling deploy is safe — old replicas still work; new replicas with the URI set snapshot at phase boundaries.

Call-site migration: places that today do `workspace_root() / task_id` directly switch to `workspace_dir_for_task(task_id, org_id=..., project_id=...)`. ~5-10 sites based on the audit. Each call site already has access to the `org_id` + `project_id` (they're on the task record).

## Out of scope

- **Agent process auto-resume** — workspaces survive pod death (restorable), but the agent subprocess does not auto-restart on a new pod. User re-triggers the task manually. v1.2 work if it becomes a real need.
- **Per-tenant configurable buckets** — `org_id → bucket` lookup table. Ships with Epic #36 Tenant Isolation if at all.
- **Retention / cleanup policies** — operators set S3 bucket-level lifecycle rules. App-managed retention is YAGNI.
- **Periodic / continuous snapshots** — final + phase-transition uploads only. Continuous mirroring would explode S3 PUT cost (git commits write thousands of `.git/objects/*` files each).
- **Azure / GCS first-class** — env-var convention only in v1.1. Typed chart blocks are a follow-up if a real pilot wants them.
- **Cross-tenant data sharing** — explicitly out per Epic #35's "decisions locked".

## Acceptance criteria (PR-close gate)

- [ ] `services/workspace_store.py` shipped with the surface above
- [ ] `from_settings()` reads `WORKSPACE_S3_URI_BASE` and falls back to local-only mode when unset
- [ ] `agent_service._safe_emit_task_status` fires snapshot uploads on the 4 named phase transitions, failure-safe
- [ ] `workspace_dir_for_task` wraps the today-implicit pattern + lazy-restores on first access
- [ ] Call sites migrated from `workspace_root() / task_id` to `workspace_dir_for_task(...)`
- [ ] Helm chart `workspaces.storage` block + validator + helm tests green
- [ ] Unit tests against fsspec's LocalFileSystem pass without network
- [ ] Integration tests pass against a real MinIO (CI service container)
- [ ] Concept doc `docs/docs/concepts/workspace-storage.md` covers operator setup (S3 / MinIO / IRSA / Azure-or-GCS env-var convention)
- [ ] Full pytest suite remains 0-fail

## Estimate

~1 week. Likely 1-2 PRs depending on review surface: PR-1 = WorkspaceStore module + agent_service hooks + workspace_service wrapper + tests + requirements; PR-2 = Helm chart + concept doc + CI service container.

## Related

- Parent Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) — Enterprise v1.1
- Parent issue [#40](https://github.com/olafkfreund/AIFactory/issues/40) — original two-half issue
- Sibling spec (half-A, shipped) — `docs/plans/2026-05-28-redis-ws-fanout-design.md`
- Cross-ref Epic [#36](https://github.com/olafkfreund/AIFactory/issues/36) — Tenant Isolation Mode (per-tenant bucket policies live there, not here)
- Built on PR [#82](https://github.com/olafkfreund/AIFactory/issues/82) — portal-managed Git clones (the workspace path that this snapshots)
