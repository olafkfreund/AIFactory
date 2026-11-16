---
status: approved
issue: 2021
intent: intent/2026-11-15-2021-harden-alembic-hook-job.md
---

# Spec: harden the alembic hook Job

## Design

All in `charts/aifactory/`, plus the guide and one test file.

1. **Deadline.** New value `migrations.jobActiveDeadlineSeconds: 300`,
   rendered as `spec.activeDeadlineSeconds` on the Job. `0` omits the field
   (no deadline, today's behaviour), and so does an absent key: an upgrade
   with `--reuse-values` keeps the old chart's defaults, which lack the key,
   so it renders no deadline rather than failing. The template uses `with`,
   not `default 300`, so both cases hold.
   The deadline bounds the Job's whole life from its start: scheduling, image
   pull, every `backoffLimit` retry and the migration itself. It is not a
   promise that Helm sees the failure before its own timeout: with Helm's
   default 5-minute `--timeout` the two race, and with the guide's
   `--timeout 10m` the Job fails about 5 minutes sooner. The values comment
   and the guide say to raise it for long migrations or slow image pulls.
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
   `global.imagePullSecrets` followed by `image.pullSecrets`, exact
   duplicates removed, or nothing when both are empty. Each list is defaulted
   to an empty list first, so a `null` value renders nothing instead of
   failing. `job-migrations.yaml` and
   `deployment.yaml` both call it in place of their two `with` blocks.
5. **Upgrade guide.** `guides/deployment/upgrade.md`: rewrite the section at
   lines 91-105 for the hook Job (what runs when, how to read its logs, what a
   failure looks like, the deadline, the `autoApply=true` mode), and correct
   the other places that assume startup migrations: line 54 (release
   notes wording), line 162 (v1.0 table row), lines 240-249 (rollback before
   the schema changed, which now means "the hook Job failed") and the
   troubleshooting table (a row for a failed or timed-out hook Job).
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
- **Allow empty resources on the Job alone** (agy): the point of item 3 is
  that the Job always has requests and limits; nobody asked for the opposite.
- **Schema entries for `jobImage` / `jobBackoffLimit`** (agy): they predate
  this task and the schema allows additional properties.
- **Chart version bump:** #2004 did not bump it; releases own the version.

## Risks

- A Job that needs longer than 300 s in total (slow pull plus a long
  migration) is killed and the upgrade fails. Today `alembic/env.py` runs all
  revisions in one transaction and no revision uses `CONCURRENTLY` or an
  autocommit block, so a killed run rolls back; a future non-transactional
  revision would not. The fix is to raise the value and re-run. Documented in
  the values comment and the guide.
- The Job now requests 500m / 1Gi by default. On a node with less free
  capacity the pod stays Pending until the deadline. `migrations.jobResources`
  is the way out.
- With `global.customCABundle.secretName` set, the Job needs that Secret at
  pre-install, and the pull Secrets too. They are operator-made, so they
  already have to exist for the Deployment.
- The CA mount helps Postgres TLS only when `DATABASE_URL` names it
  (`sslrootcert=/etc/ssl/custom-ca/<key>`): asyncpg ignores `SSL_CERT_FILE`
  for `ssl=verify-*` URLs. Same as the Deployment; the guide says so.
- Deployment pods with both pull-secret lists set now get both; with one list
  set the rendered pod spec is unchanged (checked by render diff).

## Verification

- `helm lint charts/aifactory` and `helm template` with defaults and with
  each flag on.
- `tests/helm/test_migrations_job.py`: deadline default, override and `0`;
  an absent deadline key (`=null`, the `--reuse-values` case); CA env, mount
  and volume on, off and with a custom `key`; resources fallback and
  override; pull secrets for the Job and the Deployment with both lists, a
  duplicate entry, global only, image only and neither, counting
  `imagePullSecrets:` keys in the raw text of each rendered template
  (`helm template -s`), because `yaml.safe_load` hides a duplicate key.
- Schema rejects a negative `jobActiveDeadlineSeconds`.
- `apps/backend/.venv/bin/pytest tests/helm/ -m helm` passes.
- Render diff of `deployment.yaml` before and after is empty with default
  values and with only one pull-secret list set.
- `scripts/gen_autonomy_matrix.py --check` stays clean.
- Not run on a cluster.
