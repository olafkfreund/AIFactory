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
2. No custom CA. The Deployment mounts `global.customCABundle` and gets
   `SSL_CERT_FILE` from the ConfigMap; the Job has neither. Confirmed by
   render with `global.customCABundle.secretName=corp-ca`: nothing emitted.
3. No `resources` on the Job container. A namespace ResourceQuota that
   requires limits rejects the pod. Confirmed by render.
4. With both `global.imagePullSecrets` and `image.pullSecrets` set, the key
   `imagePullSecrets` is emitted twice, so one list is lost. Confirmed by
   render, in the Job and in `templates/deployment.yaml:37-44`.
5. `guides/deployment/upgrade.md:91-97` still says web pods run alembic at
   startup. With the chart default (`migrations.autoApply=false`) the hook
   Job does.

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
  then (no chart ConfigMap, no chart ServiceAccount).
- No cluster changes; verification is by `helm lint`, `helm template` and
  `tests/helm/`.

## Open questions

None.
