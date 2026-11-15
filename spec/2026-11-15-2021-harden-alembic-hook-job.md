---
status: draft
issue: 2021
intent: intent/2026-11-15-2021-harden-alembic-hook-job.md
---

# Spec: harden the alembic hook Job

## Design

All in `charts/aifactory/`, plus the guide and one test file.

1. **Deadline.** New value `migrations.jobActiveDeadlineSeconds: 300`,
   rendered as `spec.activeDeadlineSeconds` on the Job. `0` omits the field
   (no deadline, today's behaviour). 300 s equals Helm's default `--timeout`,
   so a pod that cannot start fails as a Job (`DeadlineExceeded`) no later
   than Helm would give up, and 5 minutes earlier than the guide's
   `--timeout 10m`. The deadline also ends a migration that is still running:
   the values comment and the guide say to raise it for long migrations.
2. **Custom CA.** Under the Deployment's own condition
   (`and .Values.global .Values.global.customCABundle.secretName`) the Job
   gets the same Secret volume `custom-ca`, the same read-only mount at
   `/etc/ssl/custom-ca`, and `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` as inline
   `env` with the ConfigMap's values (`configmap.yaml:40-44`). Inline because
   the ConfigMap does not exist at pre-install.
3. **Resources.** New value `migrations.jobResources: {}`. The Job container
   renders `resources:` from it, falling back to the Deployment's
   `.Values.resources` when it is empty. So the Job always has requests and
   limits by default, and an operator with a tight quota can give the
   short-lived Job less than the 2 CPU / 4Gi web limits.
4. **Pull secrets.** New helper `aifactory.imagePullSecrets` in
   `_helpers.tpl` emits one `imagePullSecrets:` key with
   `global.imagePullSecrets` followed by `image.pullSecrets`, duplicates
   removed, or nothing when both are empty. `job-migrations.yaml` and
   `deployment.yaml` both call it in place of their two `with` blocks.
5. **Upgrade guide.** `guides/deployment/upgrade.md`: rewrite the section at
   lines 91-105 for the hook Job (what runs when, how to read its logs, what a
   failure looks like, the deadline, the `autoApply=true` mode), and correct
   the three other places that assume startup migrations (line 54 release
   notes wording, line 249 rollback, the troubleshooting table).
6. **Reference docs.** `values.schema.json` gains the two new `migrations`
   properties (`jobActiveDeadlineSeconds`: integer ≥ 0, `jobResources`:
   object). The `migrations` row in `charts/aifactory/README.md` names both
   and says the CA and pull Secrets must exist before the first install.

## Alternatives rejected

- **Hard-coded deadline** (as `teardown-cron.yaml` does): a large
  `audit_logs` migration can legitimately run longer, so it must be tunable.
- **Deadline of 600 s** to match the guide's `--timeout 10m`: it would never
  fire before Helm's timeout, which is the point of item 1.
- **`.Values.resources` with no override:** the hook runs beside the old pod
  on upgrade (`maxSurge: 0`), so a quota would have to fit a second 4Gi
  limit. Raised by agy on the intent.
- **Fixed small resources** (as `cronjob-audit-anchor.yaml` does): a
  migration's memory need depends on the release, and a wrong guess OOM-kills
  an upgrade.
- **Set `PGSSLROOTCERT` on the Job:** asyncpg would then verify the server
  for `ssl=require` URLs too, which the Deployment does not do. Parity only.
- **Use the pull-secret helper in the two CronJobs:** they read only
  `global.imagePullSecrets`, so they have no duplicate key. Giving them
  `image.pullSecrets` is a separate change, not in #2021.
- **Chart version bump:** #2004 did not bump it; releases own the version.

## Risks

- A migration longer than 300 s is killed and the upgrade fails. Uncommitted
  DDL rolls back in Postgres; the fix is to raise the value and re-run.
  Documented in the values comment and the guide.
- The Job now requests 500m / 1Gi by default. On a node with less free
  capacity the pod stays Pending until the deadline. `migrations.jobResources`
  is the way out.
- With `global.customCABundle.secretName` set, the Job needs that Secret at
  pre-install. It is operator-made, so it already has to exist for the
  Deployment.
- Deployment pods with both pull-secret lists set now get both; with one list
  set the rendered pod spec is unchanged (checked by render diff).

## Verification

- `helm lint charts/aifactory` and `helm template` with defaults and with
  each flag on.
- `tests/helm/test_migrations_job.py`: deadline default, override and `0`;
  CA env, mount and volume on and off; resources fallback and override; both
  pull-secret lists merged in the Job and in the Deployment, with exactly one
  `imagePullSecrets:` key in the raw render.
- `apps/backend/.venv/bin/pytest tests/helm/ -m helm` passes.
- Render diff of `deployment.yaml` before and after with default values is
  empty.
- `scripts/gen_autonomy_matrix.py --check` stays clean.
- Not run on a cluster.
