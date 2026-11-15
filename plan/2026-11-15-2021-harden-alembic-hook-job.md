---
status: draft
issue: 2021
spec: spec/2026-11-15-2021-harden-alembic-hook-job.md
coders:
  1: codex   # small template logic with whitespace traps
  2: codex   # same template file as step 1, keeps one context
  3: coder   # pytest cases against a fixed render, no tricky logic
  4: coder   # prose rewrite of one guide
reviewers:
  - codex    # independent read of the Helm templates
  - agy      # second model, different blind spots
---

# Plan: harden the alembic hook Job

Branch `chore/2021-harden-alembic-hook-job` from `origin/dev`. Chart is
`charts/aifactory/`. Line numbers are as of bd4a9281.

Approved decisions (from the spec):

- `migrations.jobActiveDeadlineSeconds: 300` → Job `spec.activeDeadlineSeconds`.
  `0` or an absent key (the `--reuse-values` case) renders no deadline. Use
  `with`, never `default 300`.
- Custom CA on the Job under the Deployment's condition: Secret volume
  `custom-ca`, read-only mount `/etc/ssl/custom-ca`, inline env
  `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`. No `PGSSLROOTCERT`.
- `migrations.jobResources: {}` → Job container `resources`, falling back to
  `.Values.resources` when empty.
- One helper `aifactory.imagePullSecrets` merges `global.imagePullSecrets`
  then `image.pullSecrets`, exact duplicates removed; used by the Job and the
  Deployment only (not the CronJobs).
- Guide, README row and `values.schema.json` updated. No chart version bump.

## Steps

1. Pull-secret helper.
   - `templates/_helpers.tpl`, append:

     ```
     {{/*
     One imagePullSecrets key from both lists. Two separate `with` blocks emit
     the key twice when both are set (AIFactory#2021).
     */}}
     {{- define "aifactory.imagePullSecrets" -}}
     {{- with concat ((.Values.global).imagePullSecrets | default list) (.Values.image.pullSecrets | default list) | uniq }}
     imagePullSecrets:
       {{- toYaml . | nindent 2 }}
     {{- end }}
     {{- end }}
     ```

   - `templates/deployment.yaml:37-44` and `templates/job-migrations.yaml:26-33`:
     replace the two `with` blocks by

     ```
     {{- with include "aifactory.imagePullSecrets" . }}
     {{- . | trim | nindent 6 }}
     {{- end }}
     ```

   → verify: `helm template t charts/aifactory -s templates/deployment.yaml`
   is byte-identical to `origin/dev` with default values, with only
   `global.imagePullSecrets[0].name=g`, and with only
   `image.pullSecrets[0].name=i`; with both set it has one
   `imagePullSecrets:` key listing `g` then `i`.
   Traps: without `trim` the helper leaves a blank line; `helm template`
   needs `helm dep update charts/aifactory/` first (`charts/` is gitignored).

2. Job deadline, CA and resources.
   - `templates/job-migrations.yaml`:
     - under `spec:` before `backoffLimit`:
       `{{- with .Values.migrations.jobActiveDeadlineSeconds }}` /
       `activeDeadlineSeconds: {{ . }}` / `{{- end }}`.
     - container `env`, after `PYTHONDONTWRITEBYTECODE`, inside
       `{{- if and .Values.global .Values.global.customCABundle.secretName }}`:
       `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`, both
       `/etc/ssl/custom-ca/{{ .Values.global.customCABundle.key }}` (the
       values in `configmap.yaml:42-43`).
     - container: `resources:` with
       `{{- toYaml (.Values.migrations.jobResources | default .Values.resources) | nindent 12 }}`.
     - `volumeMounts` / `volumes`, same condition: mount `custom-ca` at
       `/etc/ssl/custom-ca` `readOnly: true`; volume `custom-ca` from
       `secret.secretName: {{ .Values.global.customCABundle.secretName }}`
       (copy `deployment.yaml:803-807` and `929-933`).
   - `values.yaml:244-253` (`migrations:`): add
     `jobActiveDeadlineSeconds: 300` and `jobResources: {}` with comments:
     the deadline covers scheduling, image pull, retries and the migration;
     raise it for long migrations; `0` disables; empty `jobResources` uses
     `resources`; the DB, CA and pull Secrets must exist before the first
     install.
   - `values.schema.json:120-127` (`migrations.properties`): add
     `jobActiveDeadlineSeconds` (`integer`, `minimum: 0`) and `jobResources`
     (`object`).
   - `README.md:39` (`migrations` row): name both values, and extend the
     existing DB-Secret precondition to the CA Secret
     (`global.customCABundle.secretName`) and the pull Secrets.

   → verify: `helm lint charts/aifactory`; `helm template` with defaults shows
   `activeDeadlineSeconds: 300` and the web resources on the Job; with
   `global.customCABundle.secretName=corp-ca` shows both env vars, the mount
   and the volume; `--set migrations.jobActiveDeadlineSeconds=-1` fails
   schema validation.
   Traps: the Job must not use `envFrom` the ConfigMap or set
   `serviceAccountName`: neither exists at pre-install.

3. Tests, `tests/helm/test_migrations_job.py`: add cases for
   - deadline: default 300, override, `0` omitted, `=null` omitted;
   - CA: off by default; on gives both env vars, mount and volume; a custom
     `global.customCABundle.key` appears in both env values;
   - resources: default equals the Deployment container's; override wins;
   - pull secrets, for the Job and the Deployment: both lists → `[g, i]`; a
     duplicate entry appears once; global only; image only; neither → no key;
     each list `=null` alone, and one `=null` beside a populated one, renders
     without error.
     Count `imagePullSecrets:` in the raw output of
     `helm template -s templates/<file>` (0 or 1), because `yaml.safe_load`
     hides a duplicate key;
   - a negative deadline makes `helm template` exit non-zero.

   → verify: `apps/backend/.venv/bin/pytest tests/helm/ -m helm -q`.
   Traps: tests carry `@pytest.mark.helm` and take the `helm_available`
   fixture; ruff 0.14.10 formatting applies.

4. Guide, `guides/deployment/upgrade.md`:
   - lines 91-105: retitle to "Database migrations run in a hook Job" and
     rewrite: with the chart default (`migrations.autoApply=false`) a
     pre-install/pre-upgrade Job runs `alembic upgrade head` before pods roll;
     a failure fails `helm upgrade` and the old pods keep serving; logs are
     `kubectl -n aifactory logs -f job/aifactory-migrations` while it runs or
     after a failure (a successful Job is deleted). That is the name for the
     guide's release `aifactory`: `aifactory.fullname` does not repeat the
     chart name. Say that other release names give
     `<release>-aifactory-migrations`, and give
     `kubectl -n aifactory get jobs -l app.kubernetes.io/component=migrations`
     to find it. The Job is bounded by
     `migrations.jobActiveDeadlineSeconds` (300 s from Job start, covering
     scheduling, image pull, retries and the migration): raise it together
     with `--timeout` for long migrations or slow pulls; with Helm's default
     5-minute `--timeout` Helm may give up first; a run killed at the deadline
     rolls back (one transaction), so raise the value and re-run; the Job
     requests the web `resources` unless `migrations.jobResources` is set, so
     a pod `Pending` for lack of capacity also ends at the deadline; with `autoApply=true` (POC) the web pod
     migrates at startup as before; an upgrade with `--reuse-values` from a
     chart older than this change gets no deadline until the value is set;
     Postgres TLS with a private CA needs
     `sslrootcert=/etc/ssl/custom-ca/<key>` in `DATABASE_URL`.
   - line 54: "whether a migration runs" (drop "at startup").
   - line 162: "Auto-applied by the migration hook Job (or at startup with
     `autoApply=true`)".
   - lines 240-249: the clean-rollback case is "the migration hook Job
     failed", and only that: nothing was committed. If the hook succeeded and
     the new pods then failed to start, the schema is already at the new
     head; say so and point to "Rollback after schema change applied".
   - troubleshooting table: add a row for `helm upgrade` failing on the
     pre-upgrade hook (`DeadlineExceeded`). Causes by pod state: missing DB
     Secret → `CreateContainerConfigError`; missing CA Secret →
     `ContainerCreating` with a `FailedMount` event; missing or wrong pull
     Secret → `ImagePullBackOff`; no node capacity → `Pending`; otherwise a
     migration longer than the deadline. Fix: create the Secret, set
     `migrations.jobResources`, or raise the deadline.

   → verify: read the rendered diff; the Job name matches
   `helm template aifactory charts/aifactory -s templates/job-migrations.yaml`.
   Traps: keep the table's column alignment (markdownlint in pre-commit).

## Tests

- `helm dep update charts/aifactory/` then `helm lint charts/aifactory`.
- `apps/backend/.venv/bin/pytest tests/helm/ -m helm -q` passes.
- `apps/backend/.venv/bin/ruff check tests/helm/test_migrations_job.py` and
  `ruff format --check` on the same file.
- `apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py --check`
  clean (regenerate, never hand-edit, if it is not).
- Not run on a cluster.

## Rollback

Revert the PR. The two new values are additive; removing them restores the
previous render except that the Deployment again emits a duplicate key when
both pull-secret lists are set.
