---
status: approved
issue: 2003
spec: spec/2026-10-31-2003-chart-alembic-job.md
coders:
  1: codex   # lane brief: Codex is this lane's coder
  2: codex
  3: codex
---

# Plan: chart creates the alembic Job it promises for autoApply=false

Approved decisions (from the spec):
- New Helm hook Job `charts/aifactory/templates/job-migrations.yaml`, rendered
  only when `.Values.migrations.autoApply` is false.
  Annotations: `helm.sh/hook: pre-install,pre-upgrade`, `helm.sh/hook-weight: "-5"`,
  `helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded`.
- Image `.Values.migrations.jobImage | default (include "aifactory.image" .)`,
  `imagePullPolicy: .Values.image.pullPolicy`, same pull secrets as the Deployment.
- Command `["/home/projects/MagesticAI/.venv/bin/python", "-m", "alembic", "upgrade", "head"]`
  (image WORKDIR is apps/web-server, which holds alembic.ini).
- Env only: `DATABASE_URL` from `secretKeyRef` name `.Values.postgres.externalSecretName`,
  key `database-url` (same as deployment.yaml), and `PYTHONDONTWRITEBYTECODE=1`.
  No ConfigMap (does not exist at pre-install).
- Pod labels `aifactory.jobPodLabels` with component `migrations` (never
  selectorLabels). No `serviceAccountName`; `automountServiceAccountToken: false`.
  `podSecurityContext` / `containerSecurityContext` from values, nodeSelector and
  tolerations as the Deployment, `/tmp` emptyDir mount, `restartPolicy: Never`,
  `backoffLimit: .Values.migrations.jobBackoffLimit`.
- autoApply=true renders nothing new.

## Steps

1. charts/aifactory/templates/job-migrations.yaml (new): the Job above, styled
   like templates/pre-install-cni-probe.yaml (comment header, labels block).
   → verify by `helm lint charts/aifactory && helm template t charts/aifactory | grep -A3 'hook": pre-install,pre-upgrade'`
   Traps: never use selectorLabels on Job pods (AIFactory#1107); the chart SA
   does not exist at pre-install.
2. charts/aifactory/values.yaml `migrations:` block (~line 244) and
   charts/aifactory/README.md `migrations` row (~line 39): rewrite the comment
   to say the pre-install/pre-upgrade hook Job runs `alembic upgrade head`
   before pods roll (no init container), and that the DB Secret must exist
   before the first install (ExternalSecret-/bundled-managed Secrets do not
   exist yet at pre-install: pre-create it or install once with autoApply=true).
   → verify by `helm lint charts/aifactory`
   Traps: comment-only, keep keys and defaults unchanged.
3. tests/helm/test_migrations_job.py (new), same shape as
   tests/helm/test_audit_anchor_toggle.py (`@pytest.mark.helm`, `_render`):
   default render has exactly one Job with component `migrations`, hook
   annotation `pre-install,pre-upgrade`, command ending `upgrade head`,
   DATABASE_URL secretKeyRef `aifactory-db`/`database-url`,
   automountServiceAccountToken false; `migrations.autoApply=true` renders no
   such Job; `migrations.jobImage=x/y:z` sets the image.
   → verify by `.venv/bin/python -m pytest tests/helm/test_migrations_job.py -q`
   Traps: the test must fail on origin/dev (no Job).

## Deviations

- Added `--set migrations.autoApply=true` to the `helm install` in
  `.github/workflows/gvisor-smoke.yml`. That smoke installs with
  `postgres.bundled=true` and a busybox image, so the new pre-install hook
  Job (no python, Secret created later by the release) would fail the
  install. Bundled/POC mode is exactly where the values comment says to use
  autoApply=true; `tests/helm/test_p4_helm.py` kind install already does.

## Tests

- `helm lint charts/aifactory` passes.
- `.venv/bin/python -m pytest tests/helm -q` passes.

## Rollback

Revert the PR; the template and test are additive and the values change is comment-only.
