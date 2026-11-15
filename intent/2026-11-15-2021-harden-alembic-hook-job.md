---
status: draft
issue: 2021
author: olafkfreund
---

# Intent: harden the alembic hook Job

## Problem

The pre-install/pre-upgrade migration Job from #2004
(`charts/aifactory/templates/job-migrations.yaml`) has five gaps, found in
that PR's final review. Each was checked against `origin/dev` (bd4a9281):

1. No `activeDeadlineSeconds`. A pod stuck in CreateContainerConfigError
   (missing DB Secret) never trips `backoffLimit`, so the release hangs until
   the Helm timeout. Confirmed by render: the field is absent.
2. No custom CA. The Deployment mounts `global.customCABundle` at
   `/etc/ssl/custom-ca` and gets `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`
   from the ConfigMap; the Job has none of the three. Confirmed by render
   with `global.customCABundle.secretName=corp-ca`: nothing emitted.
   Which part matters, read from asyncpg 0.32 `connect_utils.py:686-745`:
   for an `ssl=verify-*` URL asyncpg ignores `SSL_CERT_FILE` and reads
   `sslrootcert` from the URL (or `PGSSLROOTCERT`). So a `DATABASE_URL` with
   `sslrootcert=/etc/ssl/custom-ca/<key>` works in the Deployment and fails in
   the Job because the file is not mounted. The mount is the real gap; the
   two env vars are parity. Not run against a TLS Postgres.
3. No `resources` on the Job container. A namespace ResourceQuota that
   requires limits rejects the pod. Confirmed by render.
4. With both `global.imagePullSecrets` and `image.pullSecrets` set, the key
   `imagePullSecrets` is emitted twice. That is a duplicate YAML key: a
   lenient parser keeps one list and drops the other, a strict one rejects
   the manifest. Confirmed by render, in the Job and in
   `templates/deployment.yaml:37-44`.
5. `guides/deployment/upgrade.md:91-105` still says web pods run alembic at
   startup, and its `kubectl logs deploy/aifactory-web` command looks in the
   wrong place. With the chart default (`migrations.autoApply=false`) the
   hook Job does.

## Proposed outcome

- A hook Job that cannot start fails at a bounded, configurable deadline.
- The Job trusts the same CA bundle as the Deployment.
- The Job container carries resource requests and limits.
- Both pull-secret lists reach the Job pod and the Deployment pod.
- The upgrade guide describes the hook Job and the `autoApply=true` mode.

## Affected users and systems

Operators installing or upgrading the `aifactory` Helm chart. No application
code changes.

## Constraints

- Mirror what the Deployment already does; avoid new values where an existing
  one fits.
- Default render must stay valid; existing `tests/helm/` must pass.
- The Job runs at pre-install, so it may only reference objects that exist
  then (no chart ConfigMap, no chart ServiceAccount). The CA Secret and the
  pull Secrets are operator-made, like the DB Secret, and must exist before
  the first install.
- The hook Job runs beside the old pod on upgrade, so its resources count
  against a namespace quota on top of the running Deployment.
- No cluster changes; verification is by `helm lint`, `helm template` and
  `tests/helm/`.

## Open questions

None.
