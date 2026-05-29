# AIFactory upgrade guide

> Audience: Platform / SRE operators running AIFactory and moving between releases.
> Scope: Helm-based upgrades of self-hosted AIFactory. Covers v0.x → v1.0, v1.0 → v1.1, v1.1 → v1.2.
> Companion docs: [`runbook.md`](./runbook.md), [`../operations/image-mirroring.md`](../operations/image-mirroring.md), `scripts/drills/upgrade-in-place.sh`.

## TL;DR

```bash
# 0. Take a backup.
pg_dump --format=custom --file=aifactory-pre-upgrade-$(date +%F).dump \
    "$DATABASE_URL"

# 1. Helm diff to preview the change.
helm diff upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v1.1.0

# 2. Apply.
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v1.1.0 --wait

# 3. Verify.
kubectl -n aifactory exec deploy/aifactory-web -- \
    python -m server.audit verify-chain
```

The companion script `scripts/drills/upgrade-in-place.sh --dry-run` walks through these steps in CI; run it for real in your staging cluster before each production upgrade.

---

## Upgrade philosophy

AIFactory's upgrade story has three load-bearing properties:

1. **Forward-only database migrations.** Every schema change is an additive ALTER (new nullable columns, new tables, new indexes) — never a destructive DROP without a deprecation cycle. The first version of the app to use the new column is the version AFTER the version that added it. This guarantees that a brief window of `old web pod + new schema` works during a rolling upgrade.
2. **Backward-compatible opt-in feature flags.** Every new feature added in a minor release defaults to OFF. Upgrading from v1.0 to v1.1 turns nothing on; the operator explicitly opts in to each feature via Helm values.
3. **Drillable end-to-end.** `scripts/drills/upgrade-in-place.sh` exercises the same procedure CI verifies on every PR, so the documented runbook cannot diverge from a working code path.

The combination means that a v1.0 → v1.1 upgrade is a one-line image-tag change for any operator who does not want any new feature; the new schema is harmless to old pods, and the new pods refuse to enable any new feature unless told to.

---

## General upgrade flow

This flow applies to every release. Subsequent sections call out version-specific differences.

### 1. Read the release notes

Pull the GitHub release page for the target version. Look for:

- A "BREAKING" section (rare; AIFactory aims for zero between minor versions).
- Newly-required Helm values (rare; defaults are designed to avoid this).
- Newly-deprecated Helm values (call out the deprecation warning; you have one minor release to migrate).
- Schema-impact notes (every release advertises whether a migration runs at startup).

### 2. Back up Postgres

Always do this even when no schema change is expected.

```bash
pg_dump --format=custom \
    --file=aifactory-pre-upgrade-$(date +%F).dump \
    "${DATABASE_URL}"

# Verify the dump is readable.
pg_restore --list aifactory-pre-upgrade-$(date +%F).dump | head -20
```

For Cloud-managed Postgres (RDS / Cloud SQL / Azure Database), take a snapshot through the cloud console as well — that gives you a binary-level restore path with stronger consistency than `pg_dump`.

### 3. Preview the Helm diff

```bash
# Requires: helm plugin install https://github.com/databus23/helm-diff
helm diff upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v<NEXT>
```

If anything in the diff surprises you (resource removed, RBAC change, new ClusterRole), stop and read the release notes again. Capture the diff output as a record of the change.

### 4. Apply the upgrade

```bash
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v<NEXT> \
    --wait --timeout 10m
```

The `--wait` flag holds Helm until all Deployments reach `Available` — useful for catching crash-on-startup early.

### Database migrations auto-apply at startup

Web pods run `alembic upgrade head` on startup before binding the HTTP server. This means:

- The first new pod to start carries the migration cost — typically < 10 s for typical schema changes.
- Subsequent pods see the schema already at head and start immediately.
- If the migration fails, the pod CrashLoopBackOff with the alembic error in `kubectl logs`, and the old pod keeps serving until the issue is resolved.

For deployments with very large `audit_logs` tables, monitor the migration time:

```bash
kubectl -n aifactory logs -f deploy/aifactory-web | grep -i alembic
```

If a migration takes longer than 60 s, the next-iteration migration may be designed to run lazily (mark `op.add_column(..., server_default=...)` so the column is added instantly + populated in a separate background job). Release notes call this out per release.

### 5. Verify the upgrade

```bash
# All pods Ready, no CrashLoop.
kubectl -n aifactory get pods

# Health endpoint.
kubectl -n aifactory port-forward svc/aifactory-web 8080:80 &
curl -s http://localhost:8080/api/health | jq .

# Audit chain still verifies (proves the migration didn't break the chain).
kubectl -n aifactory exec deploy/aifactory-web -- python -m server.audit verify-chain

# Smoke-test: create a small task in the UI.
```

If all four pass, the upgrade is operationally complete. If any fail, run **Rollback** below.

---

## v0.x → v1.0 upgrade

The v0.x → v1.0 transition is the largest in AIFactory's history. Before doing the upgrade, decide if you want to:

- **Carry over your v0.x audit log.** v1.0 introduced the hash-chain (Epic #26 P5.2). A v0.x audit log has no `prev_hash` values; the migration backfills them — but the chain "starts" at the migration moment, so any pre-existing log is not retroactively tamper-evident.
- **Start fresh.** Some operators prefer to archive the v0.x log and start a clean v1.0 chain.

### v0.x → v1.0 procedure

```bash
# 0. Back up — non-negotiable.
pg_dump --format=custom --file=aifactory-v0-final.dump "${DATABASE_URL}"

# 1. (Optional) archive the v0.x audit log to cold storage if you intend to start fresh.

# 2. Update Helm values for the new required v1.0 keys:
#    - crypto.backend (no default in v1.0; was Fernet implicit in v0.x)
#    - oidc.issuer + oidc.clientId (required in v1.0; v0.x supported local-password fallback)

# 3. Apply the upgrade.
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --set image.tag=v1.0.0 \
    --set crypto.backend=awskms \
    --set crypto.awskms.keyId=arn:aws:kms:us-east-1:<acct>:key/<id> \
    --set oidc.issuer=https://<idp> \
    --set oidc.clientId=<id>
```

**Forward-only migration warning.** The v0.x → v1.0 schema change is forward-only — once you upgrade, you cannot run a v0.x pod against the new schema. The downgrade SQL exists in alembic history but operators must verify schema-safety before running it (some v0.x columns may have been backfilled in a way that v0.x pods would reject as constraint-violating). If you must roll back, restore from the `pg_dump` backup taken in step 0.

### v0.x → v1.0 known incompatibilities

| What changed                                    | Why                                                                                     | Migration                                                                                                   |
| ----------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Local password login removed.                    | OIDC-only simplifies CC6.1 evidence; password storage is the IdP's responsibility.       | All users must log in via OIDC. The v1.0 schema removes `users.password_hash`; v0.x dumps fail to restore.   |
| `audit_logs` gains `prev_hash` + `classification`. | Tamper-evident chain.                                                                  | Auto-applied at startup; chain "starts" at migration time for pre-existing rows.                            |
| Helm value `database.url` → `db.url`.            | Naming consistency.                                                                    | Migration script in release notes; old key warns for one release.                                            |

---

## v1.0 → v1.1 upgrade

The v1.0 → v1.1 transition is straightforward — all v1.1 features are opt-in (per Epic #35's design); upgrading without changing any value gives you v1.0 behaviour on a v1.1 image.

### v1.0 → v1.1 procedure

```bash
# Backup.
pg_dump --format=custom --file=aifactory-v1-0-final.dump "${DATABASE_URL}"

# Diff + apply.
helm diff upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v1.1.0
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v1.1.0 --wait
```

### v1.1 features (all opt-in)

| Feature                                  | Helm key to enable                                            | Reference                                                                                              |
| ---------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Tenant Isolation Mode (#36)              | `tenant.isolationEnabled=true`                                | [`../../docs/docs/concepts/tenant-isolation.md`](../../docs/docs/concepts/tenant-isolation.md)         |
| LiteLLM gateway (#38)                    | `litellm.enabled=true`                                        | [`../../docs/docs/concepts/litellm-gateway.md`](../../docs/docs/concepts/litellm-gateway.md)           |
| SAML 2.0 + SCIM 2.0 (#41)                | `saml.enabled=true` + `scim.enabled=true`                     | [`../../docs/docs/concepts/saml-scim.md`](../../docs/docs/concepts/saml-scim.md)                       |
| OTel tracing (#42)                       | `otel.enabled=true`                                           | [`../../docs/docs/concepts/observability-tracing.md`](../../docs/docs/concepts/observability-tracing.md) |
| Audit-chain anchor (#43)                 | `audit.anchor.enabled=true`                                   | [`../../docs/docs/concepts/audit-anchor.md`](../../docs/docs/concepts/audit-anchor.md)                 |
| Multi-replica fan-out (#40)              | `replicas=N + redis.enabled=true`                             | [`../../docs/docs/concepts/multi-replica.md`](../../docs/docs/concepts/multi-replica.md)               |
| S3 workspaces (#40)                      | `workspaces.backend=s3`                                       | [`../../docs/docs/concepts/workspace-storage.md`](../../docs/docs/concepts/workspace-storage.md)       |
| Bedrock / Vertex (#34)                   | `providers.bedrock.enabled=true` / `providers.vertex.enabled=true` | [`../../docs/docs/concepts/cloud-llm-routing.md`](../../docs/docs/concepts/cloud-llm-routing.md)       |
| gVisor (#37)                             | `agent.runtimeClassName=gvisor`                               | [`../../docs/docs/concepts/gvisor-sandbox.md`](../../docs/docs/concepts/gvisor-sandbox.md)             |

### v1.0 → v1.1 backward-compat reassurance

- **Default behaviour unchanged.** A v1.0 `values.yaml` re-applied against a v1.1 chart produces a working v1.0-equivalent deployment.
- **Schema additive only.** v1.1 adds new tables (`tenant_states`, `external_identities`, `audit_hooks`, `audit_anchors`, etc) and new nullable columns; nothing existing is dropped.
- **No new required Helm values.**
- **No new required cluster prerequisites for the base install.** New cluster requirements (Calico/Cilium FQDN policy, gVisor runtime) only apply if you enable the corresponding feature.

---

## v1.1 → v1.2 upgrade (preview)

v1.2 is in flight — see `docs/plans/2026-05-29-*.md` for design docs of the in-flight features. The upgrade story will mirror v1.0 → v1.1: opt-in feature flags, additive schema, no breaking changes.

### v1.2 features landing

| Feature                                              | Helm key (proposed)                                | Issue   |
| ---------------------------------------------------- | -------------------------------------------------- | ------- |
| SAML Single Logout (SP-init + IdP-init)              | `saml.slo.enabled=true`                            | #209    |
| LiteLLM scrubBeforeSend mode + Luhn-validated CC pattern | `litellm.audit.scrubOutbound=true`            | #210    |
| Claude SDK call enforcement wrapper                  | `litellm.claudeWrapper.enabled=true`               | #207    |
| Per-tenant audit-chain anchor + external publication | `audit.anchor.perTenant=true` + publish target     | #208    |

### v1.2 upgrade preview

The upgrade procedure is the same TL;DR as above; expected operator workflow:

```bash
pg_dump --format=custom --file=aifactory-v1-1-final.dump "${DATABASE_URL}"
helm diff upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v1.2.0
helm upgrade aifactory ./charts/aifactory -n aifactory \
    --reuse-values --set image.tag=v1.2.0 --wait
```

Then explicitly opt into the v1.2 features one at a time, verifying after each.

---

## Rollback

If the upgrade verification fails, roll back immediately. The longer you wait, the more new audit rows + schema-using rows accumulate that complicate the rollback story.

### Rollback within the rolling-upgrade window (no schema change yet propagated)

```bash
helm rollback aifactory <PREVIOUS_REVISION> -n aifactory
kubectl -n aifactory get pods -w
```

Find the previous revision number with `helm history aifactory -n aifactory`.

This works cleanly when the new pods never made it past the alembic startup step — the schema is unchanged, and the previous image still matches the schema.

### Rollback after schema change applied

**Downgrade migrations exist in alembic history but operators must verify schema-safety before running them.** The downgrade SQL is generated mechanically by alembic and may not handle cases where the new schema's columns have been backfilled with values that the old schema would reject.

The safer rollback path:

1. Stop traffic to the cluster (drop the ingress, or scale `aifactory-web` to 0).
2. Restore the `pg_dump` taken at step 2 of the General upgrade flow:
   ```bash
   pg_restore --clean --if-exists --no-owner \
       --dbname="${DATABASE_URL}" \
       aifactory-pre-upgrade-<DATE>.dump
   ```
3. Roll back the Helm release.
4. Re-enable traffic.

This loses any audit rows + state changes that happened between the backup and the rollback. Document the data loss in your incident report.

### Rollback constraints by version

| From → To       | Forward-only schema?     | `helm rollback` safe?                       | `pg_restore` required?                            |
| --------------- | ------------------------ | ------------------------------------------- | ------------------------------------------------- |
| v1.1.x → v1.0   | Yes                      | Only if rollback happens within minutes — before new tables grew much. | Yes for clean rollback.                           |
| v1.0.x → v0.x   | Yes — major destructive  | No.                                         | Yes; v0.x cannot run against v1.0 schema.         |
| v1.0.x → v1.0.x | Yes (patch-additive)     | Yes.                                        | Not needed.                                       |
| v1.1.x → v1.1.x | Yes (patch-additive)     | Yes.                                        | Not needed.                                       |
| v1.2.x → v1.1   | Yes                      | Only within minutes (per v1.1.x → v1.0).    | Yes for clean rollback.                           |

---

## Troubleshooting

| Symptom                                                                                  | Likely cause                                                | Fix                                                                                                                  |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Alembic migration fails with "duplicate column".                                         | Previous upgrade left the column in place; alembic state out of sync. | `alembic stamp head` from a debug pod, then redo the upgrade. Investigate why alembic state drifted.                |
| Web pod starts but `/api/health` returns 503.                                            | Some downstream (KMS, OIDC, Postgres) failed health probe.   | `kubectl logs` to see which probe failed; verify connectivity.                                                       |
| New pod runs alongside old pod for > 10 min.                                             | `--wait` timeout reached; new pod CrashLoopBackOff.          | `kubectl describe pod` on the new pod; check probes + secrets bound.                                                 |
| `verify-chain` reports `verified=False` post-upgrade.                                    | Migration inserted an internal-marker row that broke the chain — should never happen but documented for forensics. | Restore from backup; open an issue with the migration diff + verifier output.                          |
| Helm rollback succeeds but pods still on new image.                                      | Image-pull policy `IfNotPresent` cached the new image.       | Force pod restart: `kubectl rollout restart deploy/aifactory-web`.                                                  |
| Audit anchor stops emitting after upgrade.                                               | New release introduced a key-version bump; old workers don't know the new version. | `kubectl rollout restart deploy/aifactory-anchor-job` to pick up the new key version.            |

---

## Drill cadence

| Drill                                                | Cadence                                | Script                                                                            |
| ---------------------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------- |
| Upgrade-in-place dry-run in CI                       | Every PR                               | `scripts/drills/upgrade-in-place.sh --dry-run`                                    |
| Upgrade-in-place live in staging                     | Before every prod upgrade              | Run the same script without `--dry-run` in your staging cluster.                  |
| Backup + restore                                     | Quarterly                              | `scripts/drills/backup-restore.sh` (CI dry-run + manual live run quarterly)       |
| Image-mirror with cosign preservation                | Before each upgrade to a private mirror | `scripts/drills/image-mirroring.sh` (see [`../operations/image-mirroring.md`](../operations/image-mirroring.md)) |

---

## Related documentation

- [`runbook.md`](./runbook.md) — fresh deployment from scratch.
- [`../operations/image-mirroring.md`](../operations/image-mirroring.md) — mirror upgraded images to private registries.
- [`../operations/audit-trail.md`](../operations/audit-trail.md) — chain + anchor mechanics that survive upgrades.
- [`../operations/kms-rotation-runbook.md`](../operations/kms-rotation-runbook.md) — KMS root-key rotation cadence + procedure.
- All `docs/plans/2026-05-*.md` design documents — per-feature design rationale.
